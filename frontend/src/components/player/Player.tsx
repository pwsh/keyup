import {
  AudioLines,
  Download,
  Link2,
  Pause,
  Play,
  Radio,
  Settings2,
  SkipBack,
  SkipForward,
  Volume2,
  VolumeX,
  Wand2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from "react";
import { Link } from "react-router";
import { apiUrl, isMockMode } from "../../api/client";
import { HELP } from "../../help/help";
import { copyAndToast } from "../../lib/clipboard";
import { cn } from "../../lib/cn";
import { callTalkgroupLabel } from "../../lib/format";
import { SIDEBAR_PANEL, usePanelOpen } from "../../state/panels";
import { currentIndex, MAX_SPEED, MIN_SPEED, usePlayerStore } from "../../state/player";
import { Button } from "../ui/Button";
import { RangeSlider, THUMB } from "../ui/DualRange";
import { Popover } from "../ui/Popover";
import { Switch } from "../ui/Switch";
import { Tip } from "../ui/Tooltip";
import {
  resumeAudioContext,
  scheduleToneDip,
  setNormalizeEnabled,
  suspendAudioContext,
  type ToneSpan,
  webAudioSupported,
} from "./normalize";
import { formatPlayTime, playbackDuration } from "./playback";
import {
  isAudioBlocked,
  isAudioUnlocked,
  noteAudioAllowed,
  noteAudioBlocked,
  primeAudio,
  subscribeAudioUnlock,
} from "./unlock";

/**
 * The sticky player.
 *
 * It is mounted once, at the app root, so it survives navigation: starting a
 * call on the Calls page and then opening its detail page must not restart the
 * audio. Everything it shows comes from `state/player`; the only thing it owns
 * is the `<audio>` element and the effects that keep the two in step.
 *
 * The `<audio>` element is deliberately **outside** the "is a call loaded" test, so
 * there is exactly one of it for the life of the app. A phone grants permission to
 * play to an individual element, not to the page (see `unlock.ts`); an element that
 * came and went with the bar would hand that permission back every time the player
 * was closed, and auto-play of the next call off the live feed would be refused.
 */
/** The seek and volume track: 4 px of line colour inside the 24 px target. */
const SEEK_TRACK =
  "[&::-webkit-slider-runnable-track]:h-1 [&::-webkit-slider-runnable-track]:rounded-full [&::-webkit-slider-runnable-track]:bg-[var(--line-strong)] [&::-moz-range-track]:h-1 [&::-moz-range-track]:rounded-full [&::-moz-range-track]:bg-[var(--line-strong)] [&::-moz-range-progress]:h-1 [&::-moz-range-progress]:rounded-full [&::-moz-range-progress]:bg-accent";

export function Player() {
  // Match the sidebar's width so the fixed player starts at its right edge on
  // md+ instead of spanning full width and covering the sidebar's footer
  // (sign-in and theme controls).
  const [sidebarExpanded] = usePanelOpen(SIDEBAR_PANEL);
  const current = usePlayerStore((state) => state.current);
  const playing = usePlayerStore((state) => state.playing);
  const speed = usePlayerStore((state) => state.speed);
  const volume = usePlayerStore((state) => state.volume);
  const muted = usePlayerStore((state) => state.muted);
  const continuous = usePlayerStore((state) => state.continuous);
  const autoPlayNew = usePlayerStore((state) => state.autoPlayNew);
  const live = usePlayerStore((state) => state.live);
  const normalize = usePlayerStore((state) => state.normalize);
  const rawAudio = usePlayerStore((state) => state.rawAudio);
  const currentTime = usePlayerStore((state) => state.currentTime);
  const duration = usePlayerStore((state) => state.duration);
  const pendingSeek = usePlayerStore((state) => state.pendingSeek);
  const queueLength = usePlayerStore((state) => state.queue.length);
  const index = usePlayerStore(currentIndex);

  const audioRef = useRef<HTMLAudioElement | null>(null);
  const sectionRef = useRef<HTMLElement | null>(null);
  // Asked once: whether the browser has Web Audio cannot change under our feet,
  // and the answer decides whether the Normalize switch is offered at all.
  const canNormalize = useMemo(() => webAudioSupported(), []);
  const blocked = useSyncExternalStore(subscribeAudioUnlock, isAudioBlocked, () => false);
  useShortcuts();
  useGestureUnlock(audioRef);

  // Reserve room for the bar at the foot of the page. The player is `fixed`, so
  // without this it overlaps the last rows of scrollable content. Publish its live
  // height (it wraps to two rows on narrow widths) as `--player-height` on the root
  // so `<main>` can pad its scroll area by exactly that much; clear it to 0 whenever
  // no call is loaded and the bar is unmounted.
  // biome-ignore lint/correctness/useExhaustiveDependencies: current?.id is the trigger — the bar mounts/unmounts with `current`, which the ref-based body can't express; re-running on the id change is what re-measures (or clears) the height.
  useEffect(() => {
    const root = document.documentElement;
    const el = sectionRef.current;
    if (!el) {
      root.style.setProperty("--player-height", "0px");
      return;
    }
    const publish = () => root.style.setProperty("--player-height", `${el.offsetHeight}px`);
    publish();
    const observer = new ResizeObserver(publish);
    observer.observe(el);
    return () => {
      observer.disconnect();
      root.style.setProperty("--player-height", "0px");
    };
  }, [current?.id]);

  // Play state: the element is the source of truth for *whether audio comes out*,
  // the store for whether it should. A rejected play() (no gesture yet) is put
  // back into the store rather than left as a silent lie on screen.
  // biome-ignore lint/correctness/useExhaustiveDependencies: `current.id` is the dependency — a *different call* has to re-issue play() — not the whole object, which is a new reference on every list refetch.
  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    if (playing) {
      // A context suspended by the browser (or by us, on close) would feed a
      // claimed element into silence; waking it is free when there is no graph.
      resumeAudioContext();
      const promise = audio.play();
      if (promise) {
        promise.then(noteAudioAllowed, (error: unknown) => {
          usePlayerStore.setState({ playing: false });
          // A phone that has not been tapped yet refuses, by name. Every other
          // reason — a decode failure, a call whose file has gone — is the
          // element's own problem and must not put a "tap here" prompt on screen.
          const name = error instanceof Error ? error.name : "";
          if (name === "NotAllowedError") noteAudioBlocked();
        });
      }
    } else {
      audio.pause();
    }
  }, [playing, current?.id]);

  useEffect(() => {
    const audio = audioRef.current;
    if (audio) audio.playbackRate = speed;
  }, [speed]);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.volume = volume;
    audio.muted = muted;
  }, [volume, muted]);

  // Keep the graph in step with the preference. Switching it on is also done in the
  // toggle's own handler, inside the click, because that is the gesture the autoplay
  // policy will accept an `AudioContext` from; this effect is idempotent and covers
  // the reload-with-it-already-on case. The element no longer changes underneath it —
  // there is one for the life of the app — so a claim made once holds for good.
  useEffect(() => {
    setNormalizeEnabled(audioRef.current, normalize);
  }, [normalize]);

  // Park the context while the player is closed. Not on pause: see `normalize.ts`.
  const playerOpen = current !== null;
  useEffect(() => {
    if (!playerOpen) suspendAudioContext();
  }, [playerOpen]);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio || pendingSeek === null) return;
    // `currentTime` throws on a element with no loaded metadata, which is exactly
    // the state a just-selected call is in for a frame or two.
    try {
      audio.currentTime = pendingSeek;
    } catch {
      /* the seek is re-applied by the store on the next selection */
    }
    usePlayerStore.getState().seekApplied();
  }, [pendingSeek]);

  const store = usePlayerStore.getState();
  // The server serves the processed copy by default; `?raw=1` asks for the recorder's own
  // bytes. Only offered on a call that actually has a processed copy — a toggle that does
  // nothing on most rows is worse than no toggle.
  const hasProcessed = current?.processed_audio === true;
  const playingRaw = hasProcessed && rawAudio;

  // The tone envelope the §5.5 pass applied, so Normalize's compressor and make-up gain
  // cannot hand a page or a console tone back at full blast (`normalize.ts`). The spans are
  // recorded against the *original*, so the processed copy's head trim comes off them.
  const toneSpans = useMemo(() => {
    const spans = (current?.audio_meta as { tone_spans?: unknown } | null | undefined)?.tone_spans;
    return Array.isArray(spans) ? (spans as ToneSpan[]) : null;
  }, [current?.audio_meta]);
  const toneOffsetS = useMemo(() => {
    if (playingRaw) return 0;
    const head = (current?.audio_meta as { head_trim_s?: unknown } | null | undefined)?.head_trim_s;
    return typeof head === "number" && Number.isFinite(head) ? head : 0;
  }, [current?.audio_meta, playingRaw]);
  const toneDipOn = normalize && toneSpans !== null && toneSpans.length > 0;

  // The schedule is written in absolute context time, so it is rewritten whenever the
  // playhead can have jumped under it — a new call, a seek, a resumed play, a speed change —
  // as well as when the answer itself changes.
  const rescheduleToneDip = useCallback(() => {
    scheduleToneDip(audioRef.current, toneSpans, {
      enabled: toneDipOn,
      offsetS: toneOffsetS,
    });
  }, [toneSpans, toneDipOn, toneOffsetS]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: `current.id` is the trigger — a new call reloads the element and resets its playhead — not the whole object, which is a fresh reference on every list refetch.
  useEffect(() => {
    rescheduleToneDip();
  }, [rescheduleToneDip, current?.id, playing, speed]);
  const audioSrc =
    current === null ? undefined : apiUrl(`/calls/${current.id}/audio${playingRaw ? "?raw=1" : ""}`);
  const shareUrl = current === null ? "" : `${window.location.origin}/calls/${current.id}`;

  /**
   * The one element, always mounted. Its source is whatever is loaded — nothing, while
   * the player is closed, which is a perfectly ordinary state for a media element and
   * is what keeps the phone's permission attached to it across a close and re-open.
   */
  const element = (
    <audio
      ref={audioRef}
      // In mock mode there is no audio server; the element stays silent but every
      // control, the timecode and the transcript sync still behave.
      src={isMockMode() ? undefined : audioSrc}
      preload="metadata"
      onTimeUpdate={(event) => {
        const target = event.currentTarget;
        store.setTime(target.currentTime, target.duration || current?.duration_s || 0);
      }}
      onLoadedMetadata={(event) =>
        store.setTime(
          event.currentTarget.currentTime,
          event.currentTarget.duration || current?.duration_s || 0,
        )
      }
      onSeeked={rescheduleToneDip}
      onPlay={rescheduleToneDip}
      onEnded={() => store.ended()}
    >
      <track kind="captions" />
    </audio>
  );

  const total = current === null ? 0 : playbackDuration(current, { current, duration });
  const muteButton = (
    <Button
      tip={HELP["player.mute"]}
      size="iconSm"
      variant="ghost"
      aria-label={muted ? "Unmute" : "Mute"}
      onClick={() => store.toggleMute()}
    >
      {muted ? <VolumeX size={15} aria-hidden="true" /> : <Volume2 size={15} aria-hidden="true" />}
    </Button>
  );
  const volumeSlider = (
    <Tip label={HELP["player.volume"]}>
      <input
        type="range"
        min={0}
        max={1}
        step={0.05}
        value={volume}
        aria-label="Volume"
        onChange={(event) => store.setVolume(Number(event.target.value))}
        className={cn(
          "block h-6 w-full cursor-pointer appearance-none bg-transparent",
          SEEK_TRACK,
          THUMB,
          "[&::-webkit-slider-thumb]:-mt-2.5",
        )}
      />
    </Tip>
  );
  const downloadButton = (
    <Button
      tip={HELP["player.download"]}
      size="iconSm"
      variant="ghost"
      aria-label="Download this call"
      onClick={() => window.open(`${audioSrc}?download=1`, "_blank", "noreferrer")}
    >
      <Download size={15} aria-hidden="true" />
    </Button>
  );
  const shareButton = (
    <Button
      tip={HELP["player.share"]}
      size="iconSm"
      variant="ghost"
      aria-label="Copy a link to this call"
      onClick={() => {
        void copyAndToast(shareUrl, { label: "Link", body: shareUrl });
      }}
    >
      <Link2 size={15} aria-hidden="true" />
    </Button>
  );

  // One fragment, always the same shape: the element keeps its identity — and with it
  // the phone's permission to play — whether or not the bar is on screen.
  return (
    <>
      {element}
      {current === null ? null : (
        <section
          ref={sectionRef}
          // Above the phone's bottom nav, and clear of it on desktop. On md+ the left
          // edge starts past the sidebar (matching its w-52/w-14) so the player never
          // covers the sidebar's footer controls; full width on mobile.
          className={cn(
            "fixed inset-x-0 bottom-14 z-40 border-t border-line bg-surface md:bottom-0",
            sidebarExpanded ? "md:left-52" : "md:left-14",
          )}
          aria-label="Audio player"
        >
          {/* The seek strip: the bar's full width, with a 24 px tall target over a 4 px
              track, so a thumb can find it on a phone. */}
          <Tip label={HELP["player.seek"]}>
            <input
              type="range"
              min={0}
              max={Math.max(0.1, total)}
              step={0.05}
              value={Math.min(currentTime, total)}
              aria-label="Seek within the call"
              aria-valuetext={`${formatPlayTime(currentTime)} of ${formatPlayTime(total)}`}
              onChange={(event) => store.seek(Number(event.target.value))}
              className={cn(
                "-mt-3 block h-6 w-full cursor-pointer appearance-none bg-transparent px-3 md:px-4",
                SEEK_TRACK,
                THUMB,
                "[&::-webkit-slider-thumb]:-mt-2.5",
              )}
            />
          </Tip>
          <div className="mx-auto flex max-w-[1800px] flex-col gap-1.5 px-3 pb-2 md:px-4">
            {blocked ? (
              <Tip label={HELP["player.audioBlocked"]}>
                <button
                  type="button"
                  // The whole point is that this handler *is* the gesture: priming and
                  // starting both happen inside the tap, not in an effect after it.
                  onClick={() => {
                    primeAudio(audioRef.current);
                    usePlayerStore.getState().play();
                  }}
                  className="flex items-center justify-center gap-1.5 rounded-md border border-warning bg-surface-2 px-3 py-1.5 text-xs font-medium text-ink"
                >
                  <AudioLines size={13} aria-hidden="true" />
                  Tap to enable audio
                </button>
              </Tip>
            ) : null}
            {/* One control row. On a phone: play, previous, next, the title (two lines
                if it needs them, rather than "RC TRA…"), settings and close; everything
                else is in the settings popover. */}
            <div className="flex min-h-11 items-center gap-1 sm:gap-2">
              <Button
                tip={playing ? HELP["player.pause"] : HELP["player.play"]}
                variant="primary"
                size="icon"
                className="h-11 w-11 shrink-0"
                aria-label={playing ? "Pause" : "Play"}
                onClick={() => store.toggle()}
              >
                {playing ? <Pause size={18} aria-hidden="true" /> : <Play size={18} aria-hidden="true" />}
              </Button>
              <Button
                tip={HELP["player.prev"]}
                size="iconSm"
                variant="ghost"
                className="shrink-0 sm:h-9 sm:w-9"
                aria-label="Previous call"
                disabled={index <= 0}
                onClick={() => store.previous()}
              >
                <SkipBack size={16} aria-hidden="true" />
              </Button>
              <Button
                tip={HELP["player.next"]}
                size="iconSm"
                variant="ghost"
                className="shrink-0 sm:h-9 sm:w-9"
                aria-label="Next call"
                disabled={index < 0 || index >= queueLength - 1}
                onClick={() => store.next()}
              >
                <SkipForward size={16} aria-hidden="true" />
              </Button>

              <div className="min-w-0 flex-1 pl-1">
                <Link
                  to={`/calls/${current.id}`}
                  className="line-clamp-2 break-words text-sm font-medium leading-tight text-ink hover:underline sm:line-clamp-1"
                >
                  {callTalkgroupLabel(current)}
                </Link>
                <p className="flex min-w-0 items-baseline gap-2 text-xs text-ink-2">
                  <span className="min-w-0 truncate">{current.radio_label ?? "unresolved radio"}</span>
                  <Tip label={HELP["player.elapsed"]}>
                    <span className="ml-auto shrink-0 tabular-nums">
                      {formatPlayTime(currentTime)} / {formatPlayTime(total)}
                    </span>
                  </Tip>
                </p>
              </div>

              <div className="hidden shrink-0 items-center gap-1.5 lg:flex">
                <div className="w-28">
                  <RangeSlider
                    min={MIN_SPEED}
                    max={MAX_SPEED}
                    step={0.1}
                    value={speed}
                    onChange={(next) => store.setSpeed(next)}
                    label="Speed"
                    ariaLabel="Playback speed"
                    tip={HELP["player.speed"]}
                    format={(value) => `${value.toFixed(1)}x`}
                  />
                </div>
                {muteButton}
                <div className="w-20">{volumeSlider}</div>
              </div>

              <div className="flex shrink-0 items-center gap-0.5">
                <span className="hidden items-center gap-0.5 sm:flex">
                  {downloadButton}
                  {shareButton}
                </span>
                <Popover
                  align="end"
                  className="w-72"
                  trigger={
                    <Button
                      tip={HELP["player.settings"]}
                      size="iconSm"
                      variant="ghost"
                      active={continuous || autoPlayNew || live}
                      aria-label="Player settings"
                    >
                      <Settings2 size={15} aria-hidden="true" />
                    </Button>
                  }
                >
                  <div className="space-y-3">
                    <div className="grid gap-2.5">
                      <Switch
                        checked={continuous}
                        onCheckedChange={(next) => store.setContinuous(next)}
                        label="Continuous"
                        tip={HELP["player.continuous"]}
                      />
                      <Switch
                        checked={autoPlayNew}
                        onCheckedChange={(next) => store.setAutoPlayNew(next)}
                        label="Auto-play new calls"
                        tip={HELP["player.autoPlayNew"]}
                      />
                      <Switch
                        checked={canNormalize && normalize}
                        disabled={!canNormalize}
                        onCheckedChange={(next) => {
                          // Order matters: build/resume the context inside the click, then
                          // record the preference.
                          setNormalizeEnabled(audioRef.current, next);
                          // Inside the same gesture, so the dip is armed the moment the graph is.
                          scheduleToneDip(audioRef.current, toneSpans, {
                            enabled: next && toneSpans !== null && toneSpans.length > 0,
                            offsetS: toneOffsetS,
                          });
                          store.setNormalize(next);
                        }}
                        label={
                          <span className="inline-flex items-center gap-1">
                            <AudioLines size={12} aria-hidden="true" /> Normalize
                          </span>
                        }
                        tip={canNormalize ? HELP["player.normalize"] : HELP["player.normalizeUnavailable"]}
                      />
                      <Switch
                        checked={hasProcessed && !playingRaw}
                        disabled={!hasProcessed}
                        onCheckedChange={(next) => store.setRawAudio(!next)}
                        label={
                          <span className="inline-flex items-center gap-1">
                            <Wand2 size={12} aria-hidden="true" /> Processed
                          </span>
                        }
                        tip={hasProcessed ? HELP["player.processed"] : HELP["player.processedUnavailable"]}
                      />
                      <Switch
                        checked={live}
                        onCheckedChange={(next) => store.setLive(next)}
                        label={
                          <span className="inline-flex items-center gap-1">
                            <Radio size={12} aria-hidden="true" /> Follow new calls
                          </span>
                        }
                        tip={HELP["player.live"]}
                      />
                    </div>
                    {/* Speed and volume sit in the bar from lg up; below that, here. */}
                    <div className="space-y-2 border-t border-line pt-3 lg:hidden">
                      <RangeSlider
                        min={MIN_SPEED}
                        max={MAX_SPEED}
                        step={0.1}
                        value={speed}
                        onChange={(next) => store.setSpeed(next)}
                        label="Speed"
                        ariaLabel="Playback speed"
                        tip={HELP["player.speed"]}
                        format={(value) => `${value.toFixed(1)}x`}
                      />
                      <div className="flex items-center gap-1.5">
                        {muteButton}
                        <div className="min-w-0 flex-1">{volumeSlider}</div>
                      </div>
                    </div>
                    <div className="flex items-center gap-1 border-t border-line pt-3 sm:hidden">
                      {downloadButton}
                      {shareButton}
                    </div>
                    <p className="hidden border-t border-line pt-2 text-[11px] text-muted md:block">
                      Keys: space plays and pauses, j and k step through the list.
                    </p>
                  </div>
                </Popover>
                <Button
                  tip={HELP["player.close"]}
                  size="iconSm"
                  variant="ghost"
                  className="shrink-0 sm:h-9 sm:w-9"
                  aria-label="Close the player"
                  onClick={() => store.clear()}
                >
                  <X size={15} aria-hidden="true" />
                </Button>
              </div>
            </div>
          </div>
        </section>
      )}
    </>
  );
}

/**
 * Spend the first tap anywhere in the app on priming the shared `<audio>` element.
 *
 * A phone will not start media the listener did not ask for, and the permission it
 * grants belongs to one element (see `unlock.ts`). Waiting for the listener to press
 * *this* player's Play button would mean auto-play never worked for the case it exists
 * for — a call arriving on the live feed while the phone sits on the table — so the
 * gesture is taken from whatever they touched first: a row in the calls table, the Live
 * switch, a filter chip. It listens in the capture phase, on `pointerdown` and `keydown`,
 * so it runs inside the gesture and before any handler that might stop propagation, and
 * it removes itself as soon as the element is primed.
 */
function useGestureUnlock(audioRef: React.RefObject<HTMLAudioElement | null>) {
  useEffect(() => {
    if (isAudioUnlocked()) return;
    const onGesture = () => {
      primeAudio(audioRef.current);
      if (isAudioUnlocked()) detach();
    };
    const detach = () => {
      document.removeEventListener("pointerdown", onGesture, true);
      document.removeEventListener("touchend", onGesture, true);
      document.removeEventListener("keydown", onGesture, true);
    };
    document.addEventListener("pointerdown", onGesture, true);
    // iOS Safari fires `touchend` for a tap; `pointerdown` covers everything modern,
    // and the pair costs nothing because the first one to land detaches both.
    document.addEventListener("touchend", onGesture, true);
    document.addEventListener("keydown", onGesture, true);
    return detach;
  }, [audioRef]);
}

/**
 * Space plays and pauses, j and k walk the queue. They stay out of the way of
 * anyone typing: a modifier, a text field or a contenteditable and the key
 * belongs to them.
 */
const SPACE_OWNING_ROLES = new Set([
  "switch",
  "checkbox",
  "radio",
  "menuitem",
  "menuitemcheckbox",
  "menuitemradio",
  "option",
  "combobox",
  "tab",
]);

function useShortcuts() {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target;
      if (target instanceof HTMLElement) {
        if (target.isContentEditable) return;
        if (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) return;
        // A focused switch, checkbox or menu item answers Space itself — the settings
        // popover is full of switches, and Space there must flip the switch, not pause.
        const role = target.getAttribute("role");
        if (role && SPACE_OWNING_ROLES.has(role)) return;
      }
      const store = usePlayerStore.getState();
      if (store.current === null) return;
      if (event.key === " ") {
        // Space would otherwise scroll the results out from under the reader.
        event.preventDefault();
        store.toggle();
      } else if (event.key === "j") {
        store.next();
      } else if (event.key === "k") {
        store.previous();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
}
