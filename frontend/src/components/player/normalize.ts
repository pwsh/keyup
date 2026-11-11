/**
 * Live loudness leveling for the player, in the browser.
 *
 * Scanner audio is wildly uneven: a tone-out or a close-in unit pins the meter
 * while the next transmission is a whisper from a handheld in a basement. This
 * routes the player's `<audio>` element through a compressor so both land in the
 * same neighborhood — without touching a single archived file, and for every
 * call ever recorded, not just the ones ingested after some flag was flipped.
 *
 * ## The one-way trap
 *
 * `createMediaElementSource(element)` is *permanent*. From the moment it is
 * called, the element's audio no longer reaches the speakers on its own; it only
 * comes out of whatever the graph is connected to. There is no un-claiming it,
 * and destroying the node or the context leaves the element silent forever.
 *
 * Two rules fall out of that, and this module exists to keep them:
 *
 *  1. **Nothing is created until the first time Normalize is switched on.** A
 *     listener who never touches the toggle never gets an `AudioContext`, and
 *     the element plays natively with zero overhead.
 *  2. **"Off" is a bypass, never a teardown.** Once the element is claimed, off
 *     means `source → destination` — the compressor path is disconnected, the
 *     graph stays.
 *
 * Element `volume` and `muted` are applied *upstream* of the media source, so
 * the volume slider and the mute button keep working unchanged with the graph
 * live. Volume deliberately stays on the element rather than moving into a gain
 * node, so it behaves identically before and after the first enable.
 *
 * ## The graph
 *
 *     source → compressor → makeup → brickwall → ceiling → tone → destination
 *
 * `compressor` and `makeup` are the leveling proper: flatten the spread, put the
 * level back. Everything after them exists to make that lift safe, because a
 * `DynamicsCompressorNode` is a *compressor*, not a limiter —
 * its 3 ms default attack lets the leading edge of a transient straight through,
 * and 18 dB of make-up is applied to that edge along with everything else. Web
 * Audio's destination hard-clips at ±1.0, so a close-in unit or the front of a
 * page tone came out of this graph as audible crackle even though the file
 * itself was clean. `brickwall` and `ceiling` are what make the "cannot clip"
 * claim true rather than merely intended; see {@link BRICKWALL} and
 * {@link OUTPUT_CEILING}.
 */

/** How hard the compressor works. See the module comment for the reasoning. */
export const COMPRESSOR = {
  /** Speech sits well below this; tones and close-in units sit well above it. */
  threshold: -35,
  /** Wide soft knee: leveling should be inaudible, not a pumping artifact. */
  knee: 20,
  /** Aggressive — the point is to flatten a 30 dB spread, not to season it. */
  ratio: 12,
  /** Fast enough to catch the leading edge of a tone-out. */
  attack: 0.003,
  /** Slow enough not to breathe between syllables. */
  release: 0.25,
} as const;

/**
 * Make-up gain, in dB, for what the compressor took away.
 *
 * Enough to come out level with the bypass rather than merely quieter and
 * flatter: at a ratio of 12 above -35 dB, a full-scale peak leaves the
 * compressor around -32.1 dB, so +18 puts it near -14 dB out — the same
 * ballpark a listener had before they touched the switch. Anything much less
 * and turning leveling on reads as "turning the volume down".
 *
 * The trade-off, and the reason this is not 20-something: make-up lifts the
 * noise floor along with the talker. A very quiet recording whose floor sits at
 * -60 dB comes out near -19 dB, so hiss that was inaudible becomes plainly
 * audible. For scanner speech that is a fair price — an intelligible whisper
 * over a clean silence is the wrong trade — and the toggle is opt-in, so nobody
 * pays it who has not asked to.
 *
 * Clamped hard regardless, so a future edit cannot turn a leveling aid into an
 * earful. The clamp is *not* what keeps the output out of clipping — a
 * compressor with a 3 ms attack does not catch the first milliseconds of a
 * transient, and this lift is applied to whatever it let past. The brickwall
 * stage behind it is what does that.
 */
export const MAKEUP_DB = 18;
export const MAX_MAKEUP_DB = 20;

const makeupGain = (): number => 10 ** (Math.min(MAKEUP_DB, MAX_MAKEUP_DB) / 20);

/**
 * The brickwall behind the make-up gain: a second compressor, set to limit.
 *
 * Same node type, opposite job. {@link COMPRESSOR} is slow, soft-kneed and sits
 * far down at -35 dB because it is shaping a whole transmission; this one sits
 * just under the output, snaps shut in a millisecond and lets nothing through.
 *
 *  - `threshold: -6` — 6 dB under full scale, and {@link OUTPUT_CEILING} then takes
 *    another 3 dB off behind it, so the limiting is well underway long before
 *    anything in the graph could approach 1.0.
 *  - `knee: 0` — a soft knee starts bending the signal below the threshold, which
 *    is exactly what a limiter must not do: everything under it passes untouched.
 *  - `ratio: 20` — the highest a `DynamicsCompressorNode` accepts, and the closest
 *    it gets to the ∞:1 a real limiter has.
 *  - `attack: 0.001` — the reason this node exists. The leveling compressor's 3 ms
 *    is a musical attack; a transient's first milliseconds are what clip.
 *  - `release: 0.05` — short enough that one peak does not duck the syllable after
 *    it, long enough not to modulate the signal at speech frequencies.
 */
export const BRICKWALL = {
  threshold: -6,
  knee: 0,
  ratio: 20,
  attack: 0.001,
  release: 0.05,
} as const;

/**
 * The last thing in the graph before the tone dip: a fixed -3 dB trim.
 *
 * Belt to the brickwall's braces. A `DynamicsCompressorNode` at 20:1 is not ∞:1 —
 * it still passes a fraction of whatever exceeds its threshold, and it has its own
 * make-up behaviour — so its output can sit a decibel or two over -6 dB on a hard
 * transient. 0.7 puts the whole graph's worst case comfortably under 1.0, which is
 * where Web Audio's destination hard-clips.
 *
 * The cost is 3 dB of loudness, which is the correct thing to pay: the listener has
 * a volume slider and no way at all to undo clipping.
 */
export const OUTPUT_CEILING = 0.7;

/**
 * One merged tone span out of `audio_meta.tone_spans`, in the timebase of the file the
 * server stored it against.
 */
export interface ToneSpan {
  start_s: number;
  end_s: number;
  /** One of the server's pattern names; decides the depth when `attenuate_db` is absent. */
  pattern?: string;
  /** How far the §5.5 pass actually turned this span down, dB. */
  attenuate_db?: number;
}

/** One automation point, in **media** seconds: at `at`, the tone gain becomes `gain`. */
export interface GainPoint {
  at: number;
  gain: number;
}

/** Fallbacks for a span the server recorded before it stored its own depth, dB. */
export const TONE_DEPTH_DB = { page: 18, single: 24 } as const;
const PAGE_PATTERNS = new Set(["two_tone", "warble", "alert"]);

const depthOf = (span: ToneSpan): number => {
  if (typeof span.attenuate_db === "number" && Number.isFinite(span.attenuate_db)) {
    return Math.abs(span.attenuate_db);
  }
  return PAGE_PATTERNS.has(span.pattern ?? "") ? TONE_DEPTH_DB.page : TONE_DEPTH_DB.single;
};

/**
 * The gain automation that puts the tone dip back, from `from` seconds onward.
 *
 * The compressor undoes the work the §5.5 pass did. The tone-only call is a 2 s
 * console attention tone the server took down to about -33 dB in the stored file —
 * and Normalize, seeing a call whose only loud thing is that tone, compresses it and
 * then applies 18 dB of make-up, handing it straight back at full blast. So while
 * Normalize is on, the same envelope is scheduled on a gain node of its own.
 *
 * Pure on purpose: everything about *when* is arithmetic over the spans and the
 * playhead, and only the scheduling touches Web Audio. `offsetS` is the head trim, to
 * be subtracted when the element is playing the **processed** copy (the spans are
 * recorded against the original); zero when it is playing the raw file.
 *
 * A `from` that lands inside a span emits the dip immediately, which is what makes a
 * seek into the middle of a tone come out at the right level.
 */
export function toneGainPoints(
  spans: readonly ToneSpan[] | null | undefined,
  options: { offsetS?: number; from?: number } = {},
): GainPoint[] {
  const offset = options.offsetS ?? 0;
  const from = Math.max(0, options.from ?? 0);
  if (!spans || spans.length === 0) return [];
  const windows = spans
    .map((span) => ({
      start: Math.max(0, (span.start_s ?? 0) - offset),
      end: Math.max(0, (span.end_s ?? 0) - offset),
      gain: 10 ** (-depthOf(span) / 20),
    }))
    .filter((w) => Number.isFinite(w.start) && Number.isFinite(w.end) && w.end > w.start)
    .sort((a, b) => a.start - b.start);

  const points: GainPoint[] = [];
  for (const window of windows) {
    if (window.end <= from) continue;
    // A seek that lands inside a tone: the dip is already due, so it goes on now.
    points.push({ at: Math.max(from, window.start), gain: window.gain });
    points.push({ at: window.end, gain: 1 });
  }
  return points;
}

type AudioContextConstructor = new () => AudioContext;

interface Graph {
  context: AudioContext;
  /** The element the source node has claimed. It can never be handed back. */
  element: HTMLMediaElement;
  source: MediaElementAudioSourceNode;
  compressor: DynamicsCompressorNode;
  makeup: GainNode;
  /** The limiter that makes the make-up gain safe. See {@link BRICKWALL}. */
  brickwall: DynamicsCompressorNode;
  /** A fixed {@link OUTPUT_CEILING} trim, so 20:1 not being ∞:1 cannot matter. */
  ceiling: GainNode;
  /** Last in the chain: the tone dip, 1.0 whenever there is nothing to duck. */
  tone: GainNode;
}

/**
 * App-lifetime, because the graph outlives the player's mounts: closing the
 * player unmounts the `<audio>` element, and re-opening it mounts a new one that
 * has to be claimed afresh, but there is never a reason to build a second
 * `AudioContext` (browsers cap how many a page may have).
 */
let graph: Graph | null = null;

function audioContextConstructor(): AudioContextConstructor | null {
  try {
    if (typeof window === "undefined") return null;
    const scope = window as unknown as {
      AudioContext?: AudioContextConstructor;
      webkitAudioContext?: AudioContextConstructor;
    };
    return scope.AudioContext ?? scope.webkitAudioContext ?? null;
  } catch {
    return null;
  }
}

/** Whether this browser can do the leveling at all. jsdom and old Safari cannot. */
export function webAudioSupported(): boolean {
  return audioContextConstructor() !== null;
}

/**
 * The graph for `element`, building it — and claiming the element — if needed.
 *
 * Only ever called from the "switch it on" path, so an element the listener has
 * not asked to level is never claimed.
 */
function claim(element: HTMLMediaElement): Graph | null {
  if (graph?.element === element) return graph;
  const Constructor = audioContextConstructor();
  if (Constructor === null) return null;
  try {
    // Reuse the context across elements; only the source is per-element.
    const context = graph?.context ?? new Constructor();
    if (graph !== null) {
      // The previous element is unmounted and unreachable. Its source node is
      // pure garbage now, but leaving it connected would keep it alive.
      try {
        graph.source.disconnect();
      } catch {
        /* already gone */
      }
    }
    const source = context.createMediaElementSource(element);
    const compressor = context.createDynamicsCompressor();
    compressor.threshold.value = COMPRESSOR.threshold;
    compressor.knee.value = COMPRESSOR.knee;
    compressor.ratio.value = COMPRESSOR.ratio;
    compressor.attack.value = COMPRESSOR.attack;
    compressor.release.value = COMPRESSOR.release;
    const makeup = context.createGain();
    makeup.gain.value = makeupGain();
    const brickwall = context.createDynamicsCompressor();
    brickwall.threshold.value = BRICKWALL.threshold;
    brickwall.knee.value = BRICKWALL.knee;
    brickwall.ratio.value = BRICKWALL.ratio;
    brickwall.attack.value = BRICKWALL.attack;
    brickwall.release.value = BRICKWALL.release;
    const ceiling = context.createGain();
    ceiling.gain.value = OUTPUT_CEILING;
    const tone = context.createGain();
    tone.gain.value = 1;
    graph = { context, element, source, compressor, makeup, brickwall, ceiling, tone };
    return graph;
  } catch {
    // A source already exists for this element (a hot reload, a double mount),
    // or the browser refused the context. Either way: no leveling, and — since
    // nothing was rewired — no silence either.
    return null;
  }
}

/**
 * Point the graph at the compressor, or straight at the speakers.
 *
 * Every path leaves the source connected to *something*: a claimed element with
 * a dangling source is a player that looks like it is playing and makes no
 * sound.
 */
function rewire(active: Graph, enabled: boolean): void {
  active.source.disconnect();
  active.compressor.disconnect();
  active.makeup.disconnect();
  active.brickwall.disconnect();
  active.ceiling.disconnect();
  active.tone.disconnect();
  if (enabled) {
    active.source.connect(active.compressor);
    active.compressor.connect(active.makeup);
    // …and then the make-up gain is made safe, before anything else sees it: the
    // brickwall catches the transients the leveling compressor's 3 ms attack let past,
    // and the ceiling trim covers 20:1 not being ∞:1.
    active.makeup.connect(active.brickwall);
    active.brickwall.connect(active.ceiling);
    // The tone dip goes *after* the make-up gain, so what it takes off is what comes out:
    // ducking before the make-up would only hand the lift back again. Last of all, because
    // it only ever attenuates — it can never put a peak back over the ceiling.
    active.ceiling.connect(active.tone);
    active.tone.connect(active.context.destination);
  } else {
    // Off is a bypass, and the stored file already carries the dip — so the schedule is
    // cleared too, rather than left to fire against audio that does not need it.
    clearToneSchedule(active);
    active.source.connect(active.context.destination);
  }
}

/** Cancel everything pending on the tone node and put it back at unity. */
function clearToneSchedule(active: Graph): void {
  try {
    active.tone.gain.cancelScheduledValues(active.context.currentTime);
  } catch {
    /* an implementation without cancelScheduledValues; the setValueAtTime below still holds */
  }
  try {
    active.tone.gain.setValueAtTime(1, active.context.currentTime);
  } catch {
    active.tone.gain.value = 1;
  }
}

/**
 * Schedule the tone dip for the call `element` is playing, or clear it.
 *
 * Call it whenever the answer could have changed: a new call, the Normalize switch, the
 * raw/processed switch, and — because the schedule is written in absolute context time —
 * every `play` and every completed `seeking`. Cheap: it cancels and rewrites a handful of
 * points, so calling it more often than strictly necessary is the safe direction.
 *
 * Does nothing at all when the element was never claimed (Normalize has never been on),
 * which is the same rule the rest of this module keeps.
 */
export function scheduleToneDip(
  element: HTMLMediaElement | null,
  spans: readonly ToneSpan[] | null | undefined,
  options: { enabled: boolean; offsetS?: number } = { enabled: false },
): void {
  try {
    if (element === null || graph?.element !== element) return;
    const active = graph;
    clearToneSchedule(active);
    if (!options.enabled) return;
    const now = active.context.currentTime;
    const playhead = Number.isFinite(element.currentTime) ? element.currentTime : 0;
    const rate = element.playbackRate > 0 ? element.playbackRate : 1;
    for (const point of toneGainPoints(spans, { offsetS: options.offsetS, from: playhead })) {
      // Media seconds → context seconds, at whatever speed the listener is playing.
      active.tone.gain.setValueAtTime(point.gain, now + (point.at - playhead) / rate);
    }
  } catch {
    /* the dip is a nicety; never take the player down with it */
  }
}

/**
 * Apply the Normalize preference to `element`.
 *
 * Call it from the toggle's own click handler: an `AudioContext` created outside
 * a user gesture starts suspended, and `resume()` inside the gesture is what the
 * autoplay policy accepts.
 *
 * Safe to call with no element, in mock mode, and in a browser with no Web Audio
 * at all — it does nothing rather than throwing.
 */
export function setNormalizeEnabled(element: HTMLMediaElement | null, enabled: boolean): void {
  try {
    if (element === null) return;
    if (!enabled) {
      // Never claimed: the element is playing natively and must stay that way.
      // Claiming it now, only to bypass it, would be the one thing we cannot undo.
      if (graph?.element !== element) return;
      rewire(graph, false);
      return;
    }
    const active = claim(element);
    if (active === null) return;
    rewire(active, true);
    resumeAudioContext();
  } catch {
    /* leveling is a nicety; never take the player down with it */
  }
}

/**
 * Wake the context before `play()`.
 *
 * The browser suspends it on its own when a page loses a gesture, and we suspend
 * it ourselves when the player closes; either way a suspended context feeding a
 * claimed element is silence.
 */
export function resumeAudioContext(): void {
  try {
    if (graph === null || graph.context.state !== "suspended") return;
    void graph.context.resume?.()?.catch?.(() => {});
  } catch {
    /* nothing to wake */
  }
}

/**
 * Park the context while the player is closed.
 *
 * Only on close — not on pause. Resuming is asynchronous, and a scanner call is
 * often three words long, so trading the start of every transmission for a
 * sliver of idle CPU would be a bad bargain.
 */
export function suspendAudioContext(): void {
  try {
    if (graph === null || graph.context.state !== "running") return;
    void graph.context.suspend?.()?.catch?.(() => {});
  } catch {
    /* nothing to park */
  }
}

/** Tests reset the module singleton between cases. */
export function resetAudioGraph(): void {
  graph = null;
}
