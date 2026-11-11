import { Activity } from "lucide-react";
import { useEffect, useRef } from "react";
import { useSpectrumStream } from "../../api/live";
import type { SpectrumFrame } from "../../api/types";
import { ConnectionBadge } from "../layout/StatusBadges";
import { EmptyState } from "../ui/States";

/**
 * A tuner's live spectrum: a power-vs-frequency line and a scrolling waterfall, both drawn
 * on plain 2-D canvases — no chart library and no WebGL, which the archive's CSP forbids
 * anyway. The stream opens only while this component is mounted (the caller only mounts it
 * when the view is on screen), so nothing streams when nobody is looking.
 *
 * The waterfall uses the classic OpenWebRX/KiwiSDR self-blit: each new row shifts the whole
 * image down one pixel (`drawImage` of the canvas onto itself), then the newest row is
 * written at the top, one pixel per bin, colored through a precomputed 256-entry lookup.
 */

/** The dBFS window mapped across the color ramp and the line's height. */
const DB_FLOOR = -120;
const DB_CEIL = 0;
/** How many history rows the waterfall keeps. */
const WATERFALL_ROWS = 140;
const LINE_HEIGHT = 120;

/** 0..1 position of a signed-byte dBFS value within the display window. */
function norm(db: number): number {
  const t = (db - DB_FLOOR) / (DB_CEIL - DB_FLOOR);
  return t < 0 ? 0 : t > 1 ? 1 : t;
}

/** A 256-entry RGB ramp (dark → blue → cyan → green → yellow → red), built once. */
const COLORMAP = buildColormap();

type Stop = [number, number, number, number];

function buildColormap(): Uint8ClampedArray {
  const stops: Stop[] = [
    [0.0, 8, 8, 24],
    [0.25, 12, 40, 130],
    [0.5, 0, 165, 170],
    [0.68, 40, 200, 70],
    [0.85, 245, 220, 45],
    [1.0, 240, 45, 25],
  ];
  const first = stops[0] as Stop;
  const last = stops[stops.length - 1] as Stop;
  const lut = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    let a: Stop = first;
    let b: Stop = last;
    for (let s = 0; s < stops.length - 1; s++) {
      const cur = stops[s];
      const nxt = stops[s + 1];
      if (cur && nxt && t >= cur[0] && t <= nxt[0]) {
        a = cur;
        b = nxt;
        break;
      }
    }
    const span = b[0] - a[0] || 1;
    const f = (t - a[0]) / span;
    lut[i * 3] = a[1] + (b[1] - a[1]) * f;
    lut[i * 3 + 1] = a[2] + (b[2] - a[2]) * f;
    lut[i * 3 + 2] = a[3] + (b[3] - a[3]) * f;
  }
  return lut;
}

const MHz = (hz: number): string => (hz / 1e6).toFixed(3);

export function SpectrumView({
  instanceId,
  tunerKey,
  spectrumEnabled,
}: {
  instanceId: string;
  tunerKey: string;
  /** Whether the operator has turned spectrum on for this tuner (`desired.spectrum`). */
  spectrumEnabled: boolean;
}) {
  const { frame, state } = useSpectrumStream(instanceId, tunerKey, true);
  const lineRef = useRef<HTMLCanvasElement | null>(null);
  const waterfallRef = useRef<HTMLCanvasElement | null>(null);
  // The bin count the waterfall canvas is currently sized for; a change resets it.
  const rowsBins = useRef(0);

  useEffect(() => {
    if (!frame) return;
    drawLine(lineRef.current, frame);
    drawWaterfall(waterfallRef.current, frame, rowsBins);
  }, [frame]);

  const lo = frame ? frame.center_hz - frame.span_hz / 2 : 0;
  const hi = frame ? frame.center_hz + frame.span_hz / 2 : 0;

  return (
    <div className="space-y-2 p-3">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-ink">
          Spectrum · <span className="tabular-nums">{tunerKey}</span>
        </span>
        <ConnectionBadge state={state} />
      </div>

      {frame ? (
        <>
          <div className="overflow-hidden rounded-md border border-line bg-[#0a0a14]">
            <canvas
              ref={lineRef}
              className="block h-[120px] w-full text-accent"
              aria-label={`Power spectrum for tuner ${tunerKey}`}
            />
            <canvas
              ref={waterfallRef}
              className="block w-full"
              style={{ height: WATERFALL_ROWS }}
              aria-label={`Spectrum waterfall for tuner ${tunerKey}`}
            />
          </div>
          <div className="flex justify-between text-[11px] tabular-nums text-ink-2">
            <span>{MHz(lo)} MHz</span>
            <span>{MHz(frame.center_hz)} MHz</span>
            <span>{MHz(hi)} MHz</span>
          </div>
        </>
      ) : (
        <EmptyState
          title={spectrumEnabled ? "Waiting for spectrum frames…" : "Spectrum is off for this tuner"}
          hint={
            spectrumEnabled
              ? "The recorder will start streaming FFT frames on its next poll; they appear here as they arrive."
              : "Turn on Spectrum in this tuner's settings to stream a live power spectrum and waterfall."
          }
          icon={<Activity size={26} />}
          className="py-8"
        />
      )}
    </div>
  );
}

/** Redraw the power-vs-frequency trace for one frame. */
function drawLine(canvas: HTMLCanvasElement | null, frame: SpectrumFrame): void {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const bins = frame.bins;
  if (canvas.width !== bins || canvas.height !== LINE_HEIGHT) {
    canvas.width = bins;
    canvas.height = LINE_HEIGHT;
  }
  ctx.clearRect(0, 0, bins, LINE_HEIGHT);

  // Resolve the accent color from the element so the trace tracks the theme.
  const accent = getComputedStyle(canvas).color || "#3b82f6";

  ctx.beginPath();
  for (let i = 0; i < bins; i++) {
    const y = LINE_HEIGHT - norm(frame.db[i] ?? DB_FLOOR) * LINE_HEIGHT;
    if (i === 0) ctx.moveTo(0, y);
    else ctx.lineTo(i, y);
  }
  // A soft fill under the trace, then the trace itself.
  ctx.lineTo(bins - 1, LINE_HEIGHT);
  ctx.lineTo(0, LINE_HEIGHT);
  ctx.closePath();
  ctx.globalAlpha = 0.18;
  ctx.fillStyle = accent;
  ctx.fill();

  ctx.globalAlpha = 1;
  ctx.beginPath();
  for (let i = 0; i < bins; i++) {
    const y = LINE_HEIGHT - norm(frame.db[i] ?? DB_FLOOR) * LINE_HEIGHT;
    if (i === 0) ctx.moveTo(0, y);
    else ctx.lineTo(i, y);
  }
  ctx.lineWidth = 1;
  ctx.strokeStyle = accent;
  ctx.stroke();
}

/** Scroll the waterfall down one row and write the newest frame at the top. */
function drawWaterfall(
  canvas: HTMLCanvasElement | null,
  frame: SpectrumFrame,
  rowsBins: { current: number },
): void {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const bins = frame.bins;
  if (canvas.width !== bins || canvas.height !== WATERFALL_ROWS || rowsBins.current !== bins) {
    canvas.width = bins;
    canvas.height = WATERFALL_ROWS;
    rowsBins.current = bins;
    ctx.clearRect(0, 0, bins, WATERFALL_ROWS);
  }

  // Self-blit: shift the existing image down one pixel row.
  ctx.drawImage(canvas, 0, 0, bins, WATERFALL_ROWS - 1, 0, 1, bins, WATERFALL_ROWS - 1);

  // Paint the newest row at the top, one pixel per bin, through the color ramp.
  const row = ctx.createImageData(bins, 1);
  for (let i = 0; i < bins; i++) {
    const idx = Math.round(norm(frame.db[i] ?? DB_FLOOR) * 255) * 3;
    const o = i * 4;
    row.data[o] = COLORMAP[idx] ?? 0;
    row.data[o + 1] = COLORMAP[idx + 1] ?? 0;
    row.data[o + 2] = COLORMAP[idx + 2] ?? 0;
    row.data[o + 3] = 255;
  }
  ctx.putImageData(row, 0, 0);
}
