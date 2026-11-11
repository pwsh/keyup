/**
 * The audio player's state.
 *
 * The `<audio>` element itself lives in `components/player/Player.tsx`; this
 * store owns everything *around* it — which call is loaded, the queue the
 * previous/next buttons walk, and the toggles that make the player behave like a
 * scanner (continuous, auto-play-new, live, speed, normalize). Keeping it in a
 * store rather than in the Calls page means the player survives navigating to a call's
 * detail page and back, which is exactly what "keep listening while I read this"
 * requires.
 */
import { create } from "zustand";
import type { Call } from "../api/types";

const STORAGE_KEY = "keyup-player";

export const MIN_SPEED = 1;
export const MAX_SPEED = 2;

interface Persisted {
  speed: number;
  volume: number;
  muted: boolean;
  continuous: boolean;
  autoPlayNew: boolean;
  /** Live loudness leveling through Web Audio; the files are never touched. */
  normalize: boolean;
  /**
   * Play the recorder's own file rather than the processed copy the server prefers.
   * Persisted, because somebody who wants the raw audio wants it for the whole session —
   * an operator checking what the processing did, or a listener who dislikes the gate.
   */
  rawAudio: boolean;
}

const DEFAULTS: Persisted = {
  speed: 1,
  volume: 1,
  muted: false,
  continuous: false,
  autoPlayNew: false,
  normalize: true,
  rawAudio: false,
};

function readPersisted(): Persisted {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULTS;
    const parsed = JSON.parse(raw) as Partial<Persisted>;
    return {
      speed: clampSpeed(parsed.speed ?? DEFAULTS.speed),
      volume: Math.min(1, Math.max(0, parsed.volume ?? DEFAULTS.volume)),
      muted: parsed.muted ?? DEFAULTS.muted,
      continuous: parsed.continuous ?? DEFAULTS.continuous,
      autoPlayNew: parsed.autoPlayNew ?? DEFAULTS.autoPlayNew,
      normalize: parsed.normalize ?? DEFAULTS.normalize,
      rawAudio: parsed.rawAudio ?? DEFAULTS.rawAudio,
    };
  } catch {
    return DEFAULTS;
  }
}

export const clampSpeed = (speed: number): number =>
  Math.min(MAX_SPEED, Math.max(MIN_SPEED, Number.isFinite(speed) ? speed : 1));

export interface PlayerState extends Persisted {
  /** The list previous/next walk — normally whatever the current page is showing. */
  queue: Call[];
  current: Call | null;
  playing: boolean;
  /** Seconds into the current call; the transcript highlight follows this. */
  currentTime: number;
  duration: number;
  /** A seek the `<audio>` element has not applied yet, or null when there is none. */
  pendingSeek: number | null;
  /** Follow the live feed: new matching calls are appended to the queue. */
  live: boolean;

  setQueue: (calls: Call[]) => void;
  select: (call: Call, options?: { play?: boolean }) => void;
  /**
   * A fresher copy of the call already loaded — a transcript that has just landed on
   * it, a note somebody saved. It replaces the data and nothing else: the playhead,
   * the playing state and the pending seek are all left exactly as they are, because
   * this is the same call, not a new selection.
   */
  refresh: (call: Call) => void;
  clear: () => void;
  toggle: () => void;
  play: () => void;
  pause: () => void;
  next: () => void;
  previous: () => void;
  seek: (seconds: number) => void;
  seekApplied: () => void;
  setTime: (seconds: number, duration: number) => void;
  setSpeed: (speed: number) => void;
  setVolume: (volume: number) => void;
  toggleMute: () => void;
  setContinuous: (on: boolean) => void;
  setAutoPlayNew: (on: boolean) => void;
  setNormalize: (on: boolean) => void;
  setRawAudio: (on: boolean) => void;
  setLive: (on: boolean) => void;
  /** A call arrived on the live feed: queue it, and play it if asked to. */
  liveArrived: (call: Call) => void;
  /**
   * Reconciliation ruled this queued call a copy of another recorder's: take it out of
   * the queue so previous/next and continuous play never reach it. The call *currently
   * loaded* is left alone — the audio is real and somebody is listening to it.
   */
  superseded: (id: number) => void;
  ended: () => void;
}

const persist = (state: PlayerState): void => {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        speed: state.speed,
        volume: state.volume,
        muted: state.muted,
        continuous: state.continuous,
        autoPlayNew: state.autoPlayNew,
        rawAudio: state.rawAudio,
        normalize: state.normalize,
      } satisfies Persisted),
    );
  } catch {
    /* private mode: the preferences just won't persist */
  }
};

/** Index of the current call in the queue, or -1 when it is not in it. */
export const currentIndex = (state: PlayerState): number =>
  state.current === null ? -1 : state.queue.findIndex((call) => call.id === state.current?.id);

export const usePlayerStore = create<PlayerState>((set, get) => ({
  ...readPersisted(),
  queue: [],
  current: null,
  playing: false,
  currentTime: 0,
  duration: 0,
  pendingSeek: null,
  live: false,

  setQueue: (calls) => set({ queue: calls }),

  select: (call, options) =>
    set({
      current: call,
      playing: options?.play ?? true,
      currentTime: 0,
      duration: call.duration_s,
      pendingSeek: 0,
    }),

  refresh: (call) =>
    set((state) => {
      if (state.current === null || state.current.id !== call.id || state.current === call) return {};
      return { current: call };
    }),

  clear: () => set({ current: null, playing: false, currentTime: 0, pendingSeek: null }),

  toggle: () => set((state) => ({ playing: state.current !== null && !state.playing })),
  play: () => set((state) => ({ playing: state.current !== null })),
  pause: () => set({ playing: false }),

  next: () => {
    const state = get();
    const index = currentIndex(state);
    // The queue is newest-first, so "next" in listening order is the row below.
    const nextCall = state.queue[index + 1];
    if (nextCall) state.select(nextCall, { play: true });
  },

  previous: () => {
    const state = get();
    const index = currentIndex(state);
    const previousCall = index > 0 ? state.queue[index - 1] : undefined;
    if (previousCall) state.select(previousCall, { play: true });
  },

  seek: (seconds) => set({ currentTime: seconds, pendingSeek: seconds }),
  seekApplied: () => set({ pendingSeek: null }),
  setTime: (seconds, duration) => set({ currentTime: seconds, duration }),

  setSpeed: (speed) =>
    set((state) => {
      const next = { ...state, speed: clampSpeed(speed) };
      persist(next);
      return { speed: next.speed };
    }),

  setVolume: (volume) =>
    set((state) => {
      const clamped = Math.min(1, Math.max(0, volume));
      persist({ ...state, volume: clamped, muted: clamped === 0 ? state.muted : false });
      return { volume: clamped, muted: clamped === 0 ? state.muted : false };
    }),

  toggleMute: () =>
    set((state) => {
      persist({ ...state, muted: !state.muted });
      return { muted: !state.muted };
    }),

  setContinuous: (on) =>
    set((state) => {
      persist({ ...state, continuous: on });
      return { continuous: on };
    }),

  setAutoPlayNew: (on) =>
    set((state) => {
      persist({ ...state, autoPlayNew: on });
      // Auto-play-new without the live feed running would never fire, so turning
      // it on turns the feed on too.
      return { autoPlayNew: on, live: on ? true : state.live };
    }),

  setNormalize: (on) =>
    set((state) => {
      persist({ ...state, normalize: on });
      return { normalize: on };
    }),

  setRawAudio: (on) =>
    set((state) => {
      persist({ ...state, rawAudio: on });
      return { rawAudio: on };
    }),

  setLive: (on) => set((state) => ({ live: on, autoPlayNew: on ? state.autoPlayNew : false })),

  liveArrived: (call) => {
    const state = get();
    if (!state.live) return;
    // Newest first, and bounded: the live page keeps a rolling window, not a log.
    const queue = [call, ...state.queue.filter((existing) => existing.id !== call.id)].slice(0, 200);
    if (state.autoPlayNew && !state.playing) {
      set({ queue });
      state.select(call, { play: true });
      return;
    }
    set({ queue });
  },

  superseded: (id) =>
    set((state) => {
      if (state.current?.id === id) return {};
      const queue = state.queue.filter((call) => call.id !== id);
      return queue.length === state.queue.length ? {} : { queue };
    }),

  ended: () => {
    const state = get();
    if (!state.continuous) {
      set({ playing: false });
      return;
    }
    const index = currentIndex(state);
    const nextCall = state.queue[index + 1];
    if (nextCall) state.select(nextCall, { play: true });
    else set({ playing: false });
  },
}));

/** Tests reset the singleton between cases. */
export function resetPlayer(): void {
  usePlayerStore.setState({
    ...DEFAULTS,
    queue: [],
    current: null,
    playing: false,
    currentTime: 0,
    duration: 0,
    pendingSeek: null,
    live: false,
  });
}
