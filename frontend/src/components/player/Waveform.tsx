import { useCallback, useRef } from "react";
import { cn } from "../../lib/cn";
import { Tip } from "../ui/Tooltip";
import { formatPlayTime } from "./playback";

/**
 * The call's audio envelope, drawn as SVG bars.
 *
 * SVG rather than canvas because these are at most a few hundred bars, and an
 * SVG one stays crisp on a high-density screen, scales with the container and
 * needs no resize observer. It has no Play of its own: it is a seek and scrub surface
 * for the one global player. Pressing anywhere seeks there, dragging scrubs,
 * and the whole strip is also a slider for the keyboard, so seeking never depends on a
 * mouse.
 */
export function Waveform({
  peaks,
  duration,
  position,
  onSeek,
  height = 64,
  className,
  tip,
  id,
}: {
  peaks: number[];
  duration: number;
  /** Seconds into the call. */
  position: number;
  onSeek: (seconds: number) => void;
  height?: number;
  className?: string;
  tip: string;
  id?: string;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const played = duration > 0 ? Math.min(1, Math.max(0, position / duration)) : 0;

  const seekFromEvent = useCallback(
    (clientX: number) => {
      const rect = ref.current?.getBoundingClientRect();
      if (!rect || rect.width === 0) return;
      const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
      onSeek(ratio * duration);
    },
    [duration, onSeek],
  );

  // While a press is held the pointer is captured, so a drag that leaves the strip keeps
  // scrubbing until the finger or button comes up.
  const dragging = useRef(false);

  const bars = peaks.length ? peaks : new Array(60).fill(0.15);
  const barWidth = 100 / bars.length;

  return (
    <Tip label={tip}>
      <div
        ref={ref}
        id={id}
        role="slider"
        tabIndex={0}
        aria-label="Playback position"
        aria-valuemin={0}
        aria-valuemax={Math.round(duration * 10) / 10}
        aria-valuenow={Math.round(position * 10) / 10}
        aria-valuetext={`${formatPlayTime(position)} of ${formatPlayTime(duration)}`}
        className={cn("w-full cursor-pointer touch-none select-none", className)}
        onPointerDown={(event) => {
          if (event.button !== 0) return;
          dragging.current = true;
          event.currentTarget.setPointerCapture?.(event.pointerId);
          seekFromEvent(event.clientX);
        }}
        onPointerMove={(event) => {
          if (dragging.current) seekFromEvent(event.clientX);
        }}
        onPointerUp={(event) => {
          dragging.current = false;
          event.currentTarget.releasePointerCapture?.(event.pointerId);
        }}
        onPointerCancel={() => {
          dragging.current = false;
        }}
        onKeyDown={(event) => {
          const step = event.shiftKey ? 5 : 1;
          if (event.key === "ArrowRight") onSeek(Math.min(duration, position + step));
          else if (event.key === "ArrowLeft") onSeek(Math.max(0, position - step));
          else if (event.key === "Home") onSeek(0);
          else if (event.key === "End") onSeek(duration);
          else return;
          event.preventDefault();
        }}
      >
        <svg
          viewBox={`0 0 100 ${height}`}
          preserveAspectRatio="none"
          height={height}
          className="w-full"
          aria-hidden="true"
        >
          {bars.map((peak, index) => {
            const barHeight = Math.max(2, peak * (height - 4));
            const x = index * barWidth;
            const isPlayed = index / bars.length <= played;
            return (
              <rect
                // biome-ignore lint/suspicious/noArrayIndexKey: the bars are a fixed-order sampling of one signal, not a list of entities.
                key={index}
                x={x + barWidth * 0.15}
                width={barWidth * 0.7}
                y={(height - barHeight) / 2}
                height={barHeight}
                rx={Math.min(1, barWidth * 0.35)}
                className={isPlayed ? "fill-accent" : "fill-[var(--line-strong)]"}
              />
            );
          })}
          <line
            x1={played * 100}
            x2={played * 100}
            y1={0}
            y2={height}
            className="stroke-accent-ink"
            strokeWidth={0.4}
          />
        </svg>
      </div>
    </Tip>
  );
}
