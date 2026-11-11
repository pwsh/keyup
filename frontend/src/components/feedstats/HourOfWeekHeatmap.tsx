import { useMemo } from "react";
import { WEEKDAYS } from "../../api/types";
import { cn } from "../../lib/cn";
import { sequentialRamp } from "../../lib/colors";
import { useTheme } from "../../state/theme";

/**
 * A 7x24 hour-of-week grid: rows are Monday..Sunday, columns are 00..23 on the
 * **local** clock.
 *
 * A real `<table>` rather than a canvas chart, because this grid is a table: every
 * cell has a value, a person may want to read one, and a screen reader should be
 * able to walk it by row and column. Color is a *second* encoding — each cell
 * carries its number in `title` and in the cell's accessible name, so the grid is
 * still readable to someone who cannot separate two blues.
 *
 * **A cell may be `null`, and that is not zero.** The listener grid is built from
 * polls, and a window longer than the poller has been running is mostly hours
 * nobody measured; painting those as the coldest color would say "nobody listens
 * then" about hours nobody has ever looked at. An unmeasured cell gets the hatched
 * "no data" treatment and says so in its label, which is a different fact and looks
 * like one. The call grid has no nulls — this archive holds every call it recorded,
 * so an empty hour there is a measurement.
 *
 * `maxMatrix` and `samplesMatrix` are optional and only widen the tooltip: the peak
 * reading behind an average, and how many polls the average is built from, which is
 * what separates a striking cell from a believable one.
 *
 * The scale is linear from 0 to the matrix's own maximum, and the maximum is shown
 * in the legend: two heatmaps on the same page have different maxima (listeners and
 * calls are not the same unit) and pretending otherwise would make one of them lie.
 */
export function HourOfWeekHeatmap({
  matrix,
  label,
  unit,
  maxMatrix,
  samplesMatrix,
  formatValue = (value: number) => String(Math.round(value * 10) / 10),
  className,
}: {
  /** 7 rows x 24 columns. A short or ragged matrix renders the rest as "no data". */
  matrix: (number | null)[][];
  /** What the grid is of — used as the table's accessible name. */
  label: string;
  /** Said in each cell's label: "24 listeners", "12 calls". */
  unit: string;
  /** Optional peak per cell, added to the tooltip when it is higher than the average. */
  maxMatrix?: (number | null)[][];
  /** Optional sample count per cell, added to the tooltip. */
  samplesMatrix?: number[][];
  formatValue?: (value: number) => string;
  className?: string;
}) {
  const mode = useTheme();
  const ramp = useMemo(() => sequentialRamp(7, mode), [mode]);
  const max = useMemo(
    () => Math.max(0, ...matrix.flatMap((row) => row.map((value) => value ?? 0))),
    [matrix],
  );

  /** Which band of the ramp a value falls in. 0 always gets the empty surface. */
  const colorFor = (value: number): string | undefined => {
    if (!value || max <= 0) return undefined;
    const index = Math.min(ramp.length - 1, Math.floor((value / max) * ramp.length));
    return ramp[index];
  };

  return (
    <div className={cn("min-w-0", className)}>
      {/* The grid is wider than a phone; it scrolls in its own box rather than
          widening the page. */}
      <div className="overflow-x-auto">
        <table className="w-full min-w-[34rem] border-separate border-spacing-[2px] text-[10px]">
          <caption className="sr-only">{label}</caption>
          <thead>
            <tr>
              <th scope="col" className="w-8">
                <span className="sr-only">Day</span>
              </th>
              {Array.from({ length: 24 }, (_, hour) => (
                // biome-ignore lint/suspicious/noArrayIndexKey: the index *is* the hour — a fixed 0..23 axis that never reorders.
                <th key={hour} scope="col" className="font-normal text-muted tabular-nums">
                  {/* Every third hour shown, so the labels never collide; the rest are
                      still read out, so no column header is nameless. */}
                  {hour % 3 === 0 ? (
                    String(hour).padStart(2, "0")
                  ) : (
                    <span className="sr-only">{String(hour).padStart(2, "0")}</span>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {WEEKDAYS.map((day, weekday) => (
              <tr key={day}>
                <th scope="row" className="pr-1 text-right font-normal text-muted">
                  {day}
                </th>
                {Array.from({ length: 24 }, (_, hour) => {
                  const value = matrix[weekday]?.[hour] ?? null;
                  const when = `${day} ${String(hour).padStart(2, "0")}:00`;
                  const peak = maxMatrix?.[weekday]?.[hour] ?? null;
                  const samples = samplesMatrix?.[weekday]?.[hour];
                  const parts =
                    value === null
                      ? ["no data"]
                      : [
                          `${formatValue(value)} ${unit}`,
                          ...(peak !== null && peak > value ? [`peak ${formatValue(peak)}`] : []),
                          ...(samples ? [`${samples} sample${samples === 1 ? "" : "s"}`] : []),
                        ];
                  const text = `${when} — ${parts.join(", ")}`;
                  return (
                    <td
                      // biome-ignore lint/suspicious/noArrayIndexKey: the index *is* the hour of the day; the columns never reorder.
                      key={hour}
                      title={text}
                      aria-label={text}
                      data-state={value === null ? "empty" : undefined}
                      style={{ backgroundColor: colorFor(value ?? 0) }}
                      className={cn(
                        "h-4 rounded-[2px] border border-line/40",
                        value === null
                          ? // Hatched, not just pale: "unmeasured" has to be visibly a
                            // different kind of thing from "measured and low", not a
                            // lighter shade of it.
                            "bg-[repeating-linear-gradient(45deg,var(--color-surface-2)_0px,var(--color-surface-2)_2px,transparent_2px,transparent_4px)]"
                          : value
                            ? ""
                            : "bg-surface-2",
                      )}
                    />
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-[10px] text-muted">
        <span>0</span>
        <div className="flex" aria-hidden="true">
          {ramp.map((color) => (
            <span key={color} style={{ backgroundColor: color }} className="h-2 w-5" />
          ))}
        </div>
        <span className="tabular-nums">
          {formatValue(max)} {unit}
        </span>
        {matrix.some((row) => row.some((value) => value === null)) ? (
          <span className="ml-2 flex items-center gap-1">
            <span
              aria-hidden="true"
              className="h-2 w-5 rounded-[2px] border border-line/40 bg-[repeating-linear-gradient(45deg,var(--color-surface-2)_0px,var(--color-surface-2)_2px,transparent_2px,transparent_4px)]"
            />
            no data
          </span>
        ) : null}
      </div>
    </div>
  );
}
