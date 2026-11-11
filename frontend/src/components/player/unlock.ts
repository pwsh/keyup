/**
 * Getting a phone to let the player make a sound.
 *
 * Desktop browsers will play whatever a page asks them to. Phones will not: iOS Safari
 * and Android Chrome only start media that a **user gesture** asked for, and — this is
 * the part that decides the whole design — the permission is granted to *one media
 * element*, not to the page. An element that has had `play()` (or `load()`) called on it
 * inside a real tap is marked as approved and may be started programmatically from then
 * on; every other element in the document still needs its own tap.
 *
 * That is why the player keeps a single `<audio>` element mounted for the life of the
 * app rather than one per call, and why this module exists: the first tap anywhere in
 * the app is spent priming that element, so a call arriving on the live feed twenty
 * minutes later can auto-play through it without a second gesture.
 *
 * When the priming has not happened — the very first call is selected by something that
 * was not a tap, or the browser refuses anyway — `play()` rejects with `NotAllowedError`.
 * That is not an error to swallow: it is the one case where the listener has to be told
 * to tap, so it is recorded here and the player shows a "Tap to enable audio" button.
 */

/**
 * 44 bytes of silence: a valid WAV header with no samples.
 *
 * Priming needs the element to have *something* loadable — `play()` on an element with
 * no source rejects before the browser gets as far as approving it. A zero-sample clip
 * finishes instantly and makes no sound, so it is invisible to the listener even though
 * it really did play.
 */
const SILENCE = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=";

let unlocked = false;
let blocked = false;
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of listeners) listener();
}

export function subscribeAudioUnlock(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Whether a user gesture has already been spent on the shared element. */
export function isAudioUnlocked(): boolean {
  return unlocked;
}

/**
 * Whether the browser has refused a `play()` we asked for and nobody has tapped since.
 * The player reads this to offer the prompt.
 */
export function isAudioBlocked(): boolean {
  return blocked;
}

/** Called when `play()` rejects with `NotAllowedError`: the listener has to tap. */
export function noteAudioBlocked(): void {
  if (blocked) return;
  blocked = true;
  notify();
}

/**
 * Called when a `play()` actually succeeds.
 *
 * Only real audio takes the prompt down — priming is silent, and a prompt that vanished
 * on the tap that merely primed the element would leave the listener looking at a player
 * that still is not playing. A play that succeeded is also proof the element is approved,
 * which is the other half of what this module tracks.
 */
export function noteAudioAllowed(): void {
  const changed = blocked || !unlocked;
  blocked = false;
  unlocked = true;
  if (changed) notify();
}

/**
 * Spend a user gesture on `element`, marking it approved for programmatic playback.
 *
 * Idempotent and cheap after the first success, and safe to call from any handler:
 * everything it touches is restored, and a browser that refuses is simply left as it
 * was. Must be called **synchronously inside the gesture** — a `setTimeout`, a promise
 * continuation or a React passive effect is already outside it on iOS.
 */
export function primeAudio(element: HTMLAudioElement | null): void {
  if (element === null || unlocked) return;
  // An element that already holds a call is left strictly alone: the play the app is
  // about to issue for it is what approves it (`noteAudioAllowed`), and starting and
  // stopping it here would fight the listener's own tap on Play. Only the *idle*
  // element — the ordinary state while the player is closed — needs the silence.
  if (element.currentSrc !== "" || element.getAttribute("src") !== null) return;
  try {
    element.src = SILENCE;
    const wasMuted = element.muted;
    element.muted = true;
    const started = element.play();
    const settle = () => {
      element.pause();
      element.muted = wasMuted;
    };
    if (started && typeof started.then === "function") {
      void started.then(settle, () => {
        element.muted = wasMuted;
      });
    } else {
      settle();
    }
    // Optimistic: WebKit marks the element approved the moment `play()` is called
    // inside the gesture, long before any promise settles, and the next call the
    // listener picks must not be held back waiting for it.
    unlocked = true;
    notify();
  } catch {
    // jsdom, or a browser that will not play a data URI. Nothing is worse off.
  }
}

/** Tests reset the module singleton between cases. */
export function resetAudioUnlock(): void {
  unlocked = false;
  blocked = false;
  notify();
}
