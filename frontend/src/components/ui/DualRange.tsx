import { type PointerEvent as ReactPointerEvent, useState } from "react";
import { cn } from "../../lib/cn";
import { Tip } from "./Tooltip";

/**
 * Which of the two thumbs the pointer is reaching for.
 *
 * The two inputs are stacked, so whichever is painted on top wins the press. With
 * the high input always on top, a pair sitting on the same value left the low
 * thumb impossible to grab: the high one caught the press and could only be
 * dragged up. Coincident thumbs are therefore split by *side* — the pointer
 * approaching from below wants the low thumb, from above the high one — and
 * apart from that the nearer thumb wins, ties going to the low one.
 */
export function nearerThumb(pointerValue: number, low: number, high: number): "low" | "high" {
  if (low === high) return pointerValue <= low ? "low" : "high";
  return Math.abs(pointerValue - low) <= Math.abs(pointerValue - high) ? "low" : "high";
}

/**
 * A thumb is the slider's whole hit target, so it is 24 px across (WCAG 2.5.8): a
 * transparent 4 px border around the visible 16 px dot, with the dot's 2 px
 * surface-coloured ring drawn as an inset shadow so it reads as it did at 16 px.
 */
export const THUMB =
  "[&::-webkit-slider-thumb]:box-border [&::-webkit-slider-thumb]:h-6 [&::-webkit-slider-thumb]:w-6 [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:border-4 [&::-webkit-slider-thumb]:border-solid [&::-webkit-slider-thumb]:border-transparent [&::-webkit-slider-thumb]:bg-accent [&::-webkit-slider-thumb]:bg-clip-padding [&::-webkit-slider-thumb]:shadow-[inset_0_0_0_2px_var(--surface)] [&::-moz-range-thumb]:h-6 [&::-moz-range-thumb]:w-6 [&::-moz-range-thumb]:box-border [&::-moz-range-thumb]:rounded-full [&::-moz-range-thumb]:border-4 [&::-moz-range-thumb]:border-solid [&::-moz-range-thumb]:border-transparent [&::-moz-range-thumb]:bg-accent [&::-moz-range-thumb]:bg-clip-padding [&::-moz-range-thumb]:shadow-[inset_0_0_0_2px_var(--surface)]";

/** The single slider's visible 4 px track, inside an input that is 24 px tall. */
const TRACK =
  "[&::-webkit-slider-runnable-track]:h-1 [&::-webkit-slider-runnable-track]:rounded-full [&::-webkit-slider-runnable-track]:bg-line [&::-moz-range-track]:h-1 [&::-moz-range-track]:rounded-full [&::-moz-range-track]:bg-line";

/**
 * Two stacked native range inputs — keyboard-operable by default and announced
 * as two separate sliders ("from" / "to").
 */
export function DualRange({
  min,
  max,
  step = 1,
  low,
  high,
  onChange,
  format,
  labelLow,
  labelHigh,
  tip,
  className,
}: {
  min: number;
  max: number;
  step?: number;
  low: number;
  high: number;
  onChange: (low: number, high: number) => void;
  format: (value: number) => string;
  labelLow: string;
  labelHigh: string;
  tip: string;
  className?: string;
}) {
  const pct = (v: number) => ((v - min) / (max - min)) * 100;
  // Which input is on top, recomputed as the pointer crosses the track — so the
  // thumb under the cursor is always the one that will be grabbed.
  const [reaching, setReaching] = useState<"low" | "high">("high");
  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    if (rect.width === 0) return;
    const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    setReaching(nearerThumb(min + ratio * (max - min), low, high));
  };
  return (
    <Tip label={tip}>
      <div className={cn("w-full", className)}>
        <div className="mb-1 flex items-center justify-between text-xs tabular-nums text-ink-2">
          <span>{format(low)}</span>
          <span>{format(high)}</span>
        </div>
        {/* Pointer tracking only: both thumbs are real range inputs and stay
            keyboard-operable, so nothing here is a control of its own. */}
        <div className="relative h-6" onPointerMove={onPointerMove}>
          <div className="absolute inset-x-0 top-1/2 h-1 -translate-y-1/2 rounded-full bg-line" />
          <div
            className="absolute top-1/2 h-1 -translate-y-1/2 rounded-full bg-accent"
            style={{ left: `${pct(low)}%`, width: `${Math.max(0, pct(high) - pct(low))}%` }}
          />
          <input
            type="range"
            aria-label={labelLow}
            min={min}
            max={max}
            step={step}
            value={low}
            onChange={(e) => onChange(Math.min(Number(e.target.value), high), high)}
            className={cn(
              reaching === "low" ? "z-20" : "z-10",
              "pointer-events-none absolute inset-x-0 top-0 h-6 w-full appearance-none bg-transparent [&::-webkit-slider-thumb]:pointer-events-auto [&::-moz-range-thumb]:pointer-events-auto",
              THUMB,
            )}
          />
          <input
            type="range"
            aria-label={labelHigh}
            min={min}
            max={max}
            step={step}
            value={high}
            onChange={(e) => onChange(low, Math.max(Number(e.target.value), low))}
            className={cn(
              reaching === "high" ? "z-20" : "z-10",
              "pointer-events-none absolute inset-x-0 top-0 h-6 w-full appearance-none bg-transparent [&::-webkit-slider-thumb]:pointer-events-auto [&::-moz-range-thumb]:pointer-events-auto",
              THUMB,
            )}
          />
        </div>
      </div>
    </Tip>
  );
}

/** Single-value slider with the same look (playback speed, thresholds, …). */
export function RangeSlider({
  min,
  max,
  step = 1,
  value,
  onChange,
  label,
  ariaLabel,
  tip,
  format,
  className,
}: {
  min: number;
  max: number;
  step?: number;
  value: number;
  onChange: (value: number) => void;
  label: string;
  /**
   * Overrides the accessible name, which is the visible label by default. Two
   * sliders on one panel can legitimately share a visible label, and then that
   * label no longer names either of them.
   */
  ariaLabel?: string;
  tip: string;
  format?: (value: number) => string;
  className?: string;
}) {
  return (
    <Tip label={tip}>
      <label className={cn("block w-full text-xs text-ink-2", className)}>
        <span className="mb-1 flex items-center justify-between">
          <span>{label}</span>
          <span className="tabular-nums text-ink">{format ? format(value) : value}</span>
        </span>
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={value}
          aria-label={ariaLabel ?? label}
          onChange={(e) => onChange(Number(e.target.value))}
          className={cn(
            "block h-6 w-full cursor-pointer appearance-none bg-transparent",
            TRACK,
            THUMB,
            "[&::-webkit-slider-thumb]:-mt-2.5",
          )}
        />
      </label>
    </Tip>
  );
}
