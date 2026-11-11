import { Activity, FlaskConical, Radio, WifiOff } from "lucide-react";
import { useSyncExternalStore } from "react";
import { isMockMode, subscribeMockMode } from "../../api/client";
import { useIngestLag } from "../../api/hooks";
import {
  liveTransport,
  liveTransportJournal,
  subscribeLiveTransport,
  useLiveStreamState,
} from "../../api/live";
import type { ConnectionState } from "../../api/types";
import { HELP } from "../../help/help";
import { cn } from "../../lib/cn";
import { callTalkgroupLabel, formatLag } from "../../lib/format";
import { usePlayerStore } from "../../state/player";
import { Tip } from "../ui/Tooltip";

/** Re-renders when the dev fallback latches mock mode on mid-session. */
export function useMockMode(): boolean {
  return useSyncExternalStore(subscribeMockMode, isMockMode, () => false);
}

const CONNECTION_HELP: Record<ConnectionState, keyof typeof HELP> = {
  open: "app.connection.live",
  connecting: "app.connection.connecting",
  closed: "app.connection.closed",
  mock: "app.connection.mock",
};

const CONNECTION_LABEL: Record<ConnectionState, string> = {
  open: "Live",
  connecting: "Connecting",
  closed: "Offline",
  mock: "Simulated",
};

/** Re-renders when the live feed gives up on the WebSocket and falls back to SSE. */
export function useLiveTransport() {
  return useSyncExternalStore(subscribeLiveTransport, liveTransport, () => "websocket" as const);
}

const EMPTY_JOURNAL: ReturnType<typeof liveTransportJournal> = [];

/**
 * The transport's recent history, for the badge's tooltip.
 *
 * A feed that stops on a phone has nowhere to report itself — there is no console to
 * open — so the badge carries the last few transitions: the socket opening, a handshake
 * being refused, the page waking from a suspension. It is the difference between "it
 * broke" and "the socket died while the screen was off and came back".
 */
export function useLiveTransportJournal() {
  return useSyncExternalStore(subscribeLiveTransport, liveTransportJournal, () => EMPTY_JOURNAL);
}

/**
 * Said in the tooltip once the feed has fallen back, because the badge otherwise looks
 * identical and the reason — a reverse proxy in front of us that will not forward a
 * WebSocket upgrade — is a deployment fault worth naming. See docs/REVERSE-PROXY.md.
 */
const SSE_FALLBACK_NOTE =
  "The WebSocket could not be established (usually a reverse proxy that does not forward the upgrade), so the feed fell back to server-sent events.";

/**
 * A live-feed indicator. The header passes the app's real stream state (one socket per
 * app, owned by whichever page is streaming) and, separately, whether the player is
 * following it; a page passes its own feed's state.
 */
export function ConnectionBadge({
  state,
  following = false,
}: {
  state: ConnectionState;
  following?: boolean;
}) {
  const transport = useLiveTransport();
  const journal = useLiveTransportJournal();
  // Squelch amber is the one warm colour, and it means "live". Connecting is a wait,
  // not a warning, so it stays neutral; the mock badge is a real warning and keeps it.
  const tone =
    state === "open"
      ? "border-squelch text-squelch-ink"
      : state === "connecting"
        ? "border-control-border text-ink-2"
        : state === "mock"
          ? "border-warning text-ink"
          : "border-line text-ink-2";
  return (
    <Tip
      label={
        <>
          {HELP[CONNECTION_HELP[state]]}
          {following ? <> {HELP["app.connection.following"]}</> : null}
          {transport === "sse" ? <> {SSE_FALLBACK_NOTE}</> : null}
          {journal.length > 0 ? (
            <span className="mt-1 block border-t border-line/40 pt-1 text-[10px] opacity-80">
              {journal.map((entry) => (
                <span key={`${entry.at}-${entry.note}`} className="block tabular-nums">
                  {new Date(entry.at).toLocaleTimeString()} — {entry.note}
                </span>
              ))}
            </span>
          ) : null}
        </>
      }
    >
      <span
        tabIndex={0}
        className={cn(
          "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium",
          tone,
        )}
      >
        {state === "closed" ? (
          <WifiOff size={11} aria-hidden="true" />
        ) : (
          <span
            className={cn(
              "inline-block h-1.5 w-1.5 rounded-full",
              state === "open" ? "bg-squelch" : state === "mock" ? "bg-warning" : "bg-muted",
            )}
            aria-hidden="true"
          />
        )}
        {following && state === "open" ? "Following live" : CONNECTION_LABEL[state]}
      </span>
    </Tip>
  );
}

/** How stale the archive is. Amber past ten minutes, red past an hour. */
export function IngestLagBadge() {
  const { data } = useIngestLag();
  const lag = data?.ingest_lag_s ?? null;
  const tone =
    lag === null
      ? "border-line text-ink-2"
      : lag > 3600
        ? "border-critical text-critical-ink"
        : lag > 600
          ? "border-warning text-ink"
          : "border-line text-ink-2";
  return (
    <Tip label={HELP["app.ingestLag"]}>
      <span
        tabIndex={0}
        // Not on a phone: the header there is the search box's, and the lag is a detail
        // for an operator at a desk (Statistics shows it too).
        className={cn(
          "hidden items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] tabular-nums md:inline-flex",
          tone,
        )}
      >
        <Activity size={11} aria-hidden="true" />
        {formatLag(lag)}
      </span>
    </Tip>
  );
}

export function MockBadge() {
  return (
    <Tip label={HELP["app.mockBadge"]}>
      <span
        tabIndex={0}
        className="inline-flex items-center gap-1 rounded-full border border-warning px-2 py-0.5 text-[11px] text-ink"
      >
        <FlaskConical size={11} aria-hidden="true" />
        Sample data
      </span>
    </Tip>
  );
}

/**
 * What the header pill shows: the real state of the live stream, and separately whether
 * the player is following it. `state` is "closed" when no page is streaming at all, in
 * which case the header shows no pill rather than an "Offline" that reads like a fault.
 */
export function useHeaderConnection(): { state: ConnectionState; following: boolean } {
  const state = useLiveStreamState();
  const live = usePlayerStore((player) => player.live);
  return { state, following: live && state !== "closed" };
}

/** A compact "now playing" line for the header on wide screens. */
export function NowPlaying() {
  const current = usePlayerStore((state) => state.current);
  if (!current) return null;
  return (
    <Tip label={HELP["player.nowPlaying"]}>
      <span
        tabIndex={0}
        className="hidden max-w-48 items-center gap-1.5 truncate text-[11px] text-ink-2 xl:inline-flex"
      >
        <Radio size={11} aria-hidden="true" />
        {callTalkgroupLabel(current)}
      </span>
    </Tip>
  );
}
