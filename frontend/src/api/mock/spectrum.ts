/**
 * The simulated spectrum feed. Real mode opens `GET /admin/recorders/spectrum/stream` over
 * an `EventSource`; in mock mode this synthesises plausible FFT frames — a noise floor with
 * a handful of drifting carriers — so the spectrum line and waterfall have something to draw
 * with no recorder running.
 */
import type { SpectrumFrame } from "../types";

const BINS = 256;
const CENTER_HZ = 855_500_000;
const SPAN_HZ = 10_000_000;

/** A few carriers that wander a little, so the waterfall shows moving traces. */
const CARRIERS = [
  { bin: 40, height: 70, drift: 0.15 },
  { bin: 110, height: 95, drift: 0.05 },
  { bin: 150, height: 55, drift: 0.2 },
  { bin: 205, height: 82, drift: 0.1 },
];

/** One synthetic frame: a −110 dBFS-ish floor plus Gaussian bumps at each carrier. */
function synth(tick: number): number[] {
  const db = new Array<number>(BINS);
  for (let i = 0; i < BINS; i++) {
    // Floor near the bottom of the signed-byte range, with a little noise.
    let value = -118 + Math.random() * 6;
    for (const carrier of CARRIERS) {
      const center = carrier.bin + Math.sin(tick * carrier.drift + carrier.bin) * 3;
      const width = 4;
      const d = (i - center) / width;
      value += carrier.height * Math.exp(-0.5 * d * d) * (0.8 + Math.random() * 0.2);
    }
    // Quantise to a signed byte, as the recorder does.
    db[i] = Math.max(-128, Math.min(127, Math.round(value)));
  }
  return db;
}

/** A single snapshot frame — handy for tests and for the initial paint. */
export function mockSpectrumFrame(tuner = "airspy-0", instance = "trunkd-central-01"): SpectrumFrame {
  return {
    instance_id: instance,
    tuner_key: tuner,
    ts_ms: Date.now(),
    center_hz: CENTER_HZ,
    span_hz: SPAN_HZ,
    bins: BINS,
    db: synth(0),
  };
}

/**
 * Emit a fresh frame a few times a second. Returns the unsubscribe the hook hands React.
 */
export function subscribeMockSpectrum(
  instance: string,
  tuner: string,
  onFrame: (frame: SpectrumFrame) => void,
): () => void {
  let cancelled = false;
  let tick = 0;
  const timer = setInterval(() => {
    if (cancelled) return;
    tick += 1;
    onFrame({
      instance_id: instance,
      tuner_key: tuner,
      ts_ms: Date.now(),
      center_hz: CENTER_HZ,
      span_hz: SPAN_HZ,
      bins: BINS,
      db: synth(tick),
    });
  }, 250);
  return () => {
    cancelled = true;
    clearInterval(timer);
  };
}
