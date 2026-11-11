import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
import { resetMockFallback } from "../api/client";
import { resetLiveStreamState } from "../api/liveStreamState";
import { resetMockState } from "../api/mock";
import { loadHelp } from "../help/help";
import { resetSignIn } from "../state/auth";
import { resetPanelsForTest } from "../state/panelStore";
import { resetPlayer } from "../state/player";

// The help text is a lazily loaded chunk in the app (`help/help.ts`); tests read
// `HELP[...]` as the sentence it is, so it is loaded before any of them runs.
await loadHelp();

// `VITE_MOCK=1` — which points the client at the in-memory fixtures — is set in
// `vite.config.ts` under `test.env`, because it has to be in place before any
// module is imported and these statements run after that.

// jsdom has neither of these, and Radix expects both.
if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}
/**
 * jsdom has no `matchMedia` at all, and a stub that answers `false` to everything
 * is worse than none here: the responsive pages read `(min-width: …)` to choose
 * between the desktop table and the phone's cards, so a blanket `false` would
 * test only the phone layout. This answers width queries against a desktop
 * viewport and says no to everything else (reduced motion, coarse pointer).
 */
window.innerWidth = 1440;
window.innerHeight = 900;
window.matchMedia = ((query: string) => {
  const min = /\(min-width:\s*(\d+)px\)/.exec(query);
  const max = /\(max-width:\s*(\d+)px\)/.exec(query);
  const matches =
    (min ? window.innerWidth >= Number(min[1]) : true) &&
    (max ? window.innerWidth <= Number(max[1]) : true) &&
    (min !== null || max !== null);
  return {
    matches,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  };
}) as unknown as typeof window.matchMedia;

window.HTMLElement.prototype.scrollIntoView = () => {};

/**
 * jsdom performs no layout, so every element reports `offsetWidth`/`offsetHeight`
 * of 0 — and `@tanstack/react-virtual` reads exactly those to decide how many rows
 * fit in the scroll box, concludes "none", and renders an empty list. Reporting a
 * plausible viewport is what makes the virtualized tables testable at all.
 */
for (const [property, value] of [
  ["offsetWidth", 1200],
  ["offsetHeight", 800],
  ["clientWidth", 1200],
  ["clientHeight", 800],
] as const) {
  Object.defineProperty(window.HTMLElement.prototype, property, {
    configurable: true,
    get: () => value,
  });
}
window.Element.prototype.getBoundingClientRect = () =>
  ({
    width: 1200,
    height: 800,
    top: 0,
    left: 0,
    bottom: 800,
    right: 1200,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  }) as DOMRect;

// Radix's Select uses the Pointer Capture API to keep tracking a press that leaves
// the trigger; jsdom implements none of it.
window.HTMLElement.prototype.hasPointerCapture ??= () => false;
window.HTMLElement.prototype.setPointerCapture ??= () => {};
window.HTMLElement.prototype.releasePointerCapture ??= () => {};

// jsdom's <audio> is inert: play() is missing entirely, so the player's effect
// would throw instead of merely producing no sound.
window.HTMLMediaElement.prototype.play ??= () => Promise.resolve();
window.HTMLMediaElement.prototype.pause ??= () => {};

// The object-URL API, which jsdom has no implementation of at all. The audio-profile
// editor hands a rendered MP3 to an `<audio>` through one and revokes it afterwards, so
// without these the dialog throws where a browser would simply play. The counterfeit URL
// carries a counter so a second render is distinguishable from the first, which is what
// the test asserting the blob is replaced actually reads.
let objectUrls = 0;
window.URL.createObjectURL ??= () => {
  objectUrls += 1;
  return `blob:keyup/${objectUrls}`;
};
window.URL.revokeObjectURL ??= () => {};

afterEach(() => {
  cleanup();
  resetMockState();
  resetMockFallback();
  resetSignIn();
  resetPlayer();
  resetPanelsForTest();
  resetLiveStreamState();
});

/**
 * jsdom has no 2D canvas: `getContext("2d")` returns `null`, and ECharts' renderer
 * then throws `Cannot read properties of null (reading 'clearRect')` from inside
 * zrender — asynchronously, so it surfaces as an unhandled error that fails
 * whichever test happened to be running.
 *
 * The stub is a Proxy that answers every 2D-context method with a no-op and every
 * property with a harmless value, plus real answers for the handful of calls whose
 * *return* is used for layout. Nothing in this suite asserts on pixels — chart
 * behavior is tested through the option object and the table view — so a context
 * that merely does not throw is exactly enough.
 */
const canvasContext = new Proxy(
  {
    canvas: null,
    measureText: () => ({ width: 0, actualBoundingBoxAscent: 0, actualBoundingBoxDescent: 0 }),
    getImageData: () => ({ data: new Uint8ClampedArray(4), width: 1, height: 1 }),
    createImageData: () => ({ data: new Uint8ClampedArray(4), width: 1, height: 1 }),
    createLinearGradient: () => ({ addColorStop: () => {} }),
    createRadialGradient: () => ({ addColorStop: () => {} }),
    createPattern: () => null,
    getLineDash: () => [],
    isPointInPath: () => false,
    isPointInStroke: () => false,
  } as Record<string, unknown>,
  {
    get: (target, property) => (property in target ? target[property as string] : () => undefined),
    set: (target, property, value) => {
      target[property as string] = value;
      return true;
    },
  },
);
window.HTMLCanvasElement.prototype.getContext = ((kind: string) =>
  kind === "2d" ? canvasContext : null) as unknown as typeof window.HTMLCanvasElement.prototype.getContext;
window.HTMLCanvasElement.prototype.toDataURL = () => "data:image/png;base64,";
