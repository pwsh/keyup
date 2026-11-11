import { Pause, Play, Radio, Trash2 } from "lucide-react";
import { type CSSProperties, useEffect, useId, useMemo, useRef, useState } from "react";
import { Link } from "react-router";
import { useCalls, useTalkgroups } from "../api/hooks";
import { useLiveFeed } from "../api/live";
import type { Call } from "../api/types";
import { compareNewestFirst, isOlderCopy } from "../components/calls/liveCalls";
import { useFreshIds } from "../components/calls/useFreshIds";
import { PageHeader } from "../components/layout/PageHeader";
import { Button } from "../components/ui/Button";
import { Card, CardHeader } from "../components/ui/Card";
import { Chip } from "../components/ui/Chip";
import { QualityDot } from "../components/ui/QualityDot";
import { EmptyState } from "../components/ui/States";
import { Switch } from "../components/ui/Switch";
import { Tip } from "../components/ui/Tooltip";
import { HELP } from "../help/help";
import { cn } from "../lib/cn";
import { channelEdgeColor } from "../lib/color";
import {
  callTalkgroupLabel,
  formatClock,
  formatDuration,
  formatRelative,
  talkgroupLabel,
} from "../lib/format";
import { noTranscriptReason } from "../lib/transcriptReason";
import { useApiParams, useCallFilters } from "../state/filters";
import { usePlayerStore } from "../state/player";
import { useTheme } from "../state/theme";

const ACTIVE_WINDOW_MS = 10 * 60_000;

/** How much of the archive to pull back on arrival and after a suspension, as one page. */
const BACKFILL_SIZE = 50;

/** Calls read to seed "Who's active" on arrival — ten busy minutes fit in one page. */
const ACTIVE_SEED_SIZE = 500;

/** Calls as they arrive, plus who has been heard in the last ten minutes. */
export function Live() {
  const { filters, patch, query } = useCallFilters();
  const [paused, setPaused] = useState(false);
  const talkgroups = useTalkgroups();
  const live = usePlayerStore((state) => state.live);
  const autoPlayNew = usePlayerStore((state) => state.autoPlayNew);
  const setLive = usePlayerStore((state) => state.setLive);
  const setAutoPlayNew = usePlayerStore((state) => state.setAutoPlayNew);
  const liveArrived = usePlayerStore((state) => state.liveArrived);
  const supersededInPlayer = usePlayerStore((state) => state.superseded);
  const select = usePlayerStore((state) => state.select);
  const currentId = usePlayerStore((state) => state.current?.id ?? null);
  const playingId = usePlayerStore((state) => (state.playing ? (state.current?.id ?? null) : null));
  const theme = useTheme();

  // Opening this page is asking for the feed; leaving it does not stop it, because
  // the player's own Live toggle is what owns that decision.
  // biome-ignore lint/correctness/useExhaustiveDependencies: deliberately once, on mount — re-running on `live` would fight the reader every time they switched the feed off while still on this page.
  useEffect(() => {
    if (!live) setLive(true);
  }, []);

  const { params, refresh: refreshWindow } = useApiParams(filters);
  // `query` (the filters, not the params) is what clears the buffer: a new talkgroup or
  // search starts an empty list, while a rolling window re-read on resume keeps it.
  const feed = useLiveFeed(params, live && !paused, query);

  /**
   * What the stream missed while the page was asleep.
   *
   * A phone playing a call is a phone whose screen locks, and the socket goes with it.
   * Nothing is buffered for us, so on the way back the feed says so (`resumedAt`) and
   * this query fetches the recent window from the archive instead.
   *
   * It also runs on arrival: a stream only carries what happens from now on, so a page
   * that waited for it opened on "Listening…" for most of a minute while calls were
   * flowing. The newest page of the archive fills the list at once.
   */
  const backfillParams = useMemo(() => ({ ...params, page_size: BACKFILL_SIZE }), [params]);
  const backfill = useCalls(backfillParams);
  const resumedAt = feed.resumedAt;
  const backfillRefetch = backfill.refetch;
  useEffect(() => {
    if (resumedAt === null) return;
    // A rolling window is measured from now again; when that moved its start the new
    // query key fetches on its own, and only an unchanged one needs the refetch.
    if (!refreshWindow()) void backfillRefetch();
  }, [resumedAt, refreshWindow, backfillRefetch]);

  // A new filter is a clean slate, as though the page had just opened: the feed's buffer
  // clears itself (`useLiveFeed`) and the backfill asks again under the new filter. Until
  // it answers, the previous filter's page (kept as placeholder data) is not shown.
  const backfillItems = backfill.isPlaceholderData ? undefined : backfill.data?.items;

  /**
   * "Who's active" is about the last ten minutes, which the stream alone cannot answer
   * until ten minutes after the page opened. One request for that window, taken once on
   * arrival, seeds it; the stream and the list keep it current from there.
   */
  const [activeSince] = useState(() => new Date(Date.now() - ACTIVE_WINDOW_MS).toISOString());
  const activeSeedParams = useMemo(
    () => ({ ...params, date_from: activeSince, page_size: ACTIVE_SEED_SIZE }),
    [params, activeSince],
  );
  const activeSeed = useCalls(activeSeedParams);

  /**
   * Calls reconciliation has demoted: two recorders uploaded the same transmission and
   * this copy lost. The feed says so with `call.superseded` and no call object, because
   * the demoted row no longer passes the filters that would re-read it.
   */
  const superseded = useMemo(() => {
    const ids = new Set<number>();
    for (const event of feed.events) {
      if (event.type === "call.superseded") ids.add(event.id);
    }
    return ids;
  }, [feed.events]);

  // Out of the player's queue too, so previous/next and continuous play never reach a
  // copy. The call actually loaded is left where it is — the store refuses that one.
  useEffect(() => {
    for (const id of superseded) supersededInPlayer(id);
  }, [superseded, supersededInPlayer]);

  const calls = useMemo(() => {
    const seen = new Set<number>();
    const result: Call[] = [];
    /** Where each streamed call sits in `result`, for a fresher backfill copy to replace. */
    const at = new Map<number, number>();
    for (const event of feed.events) {
      if (event.type === "ingest.status" || event.type === "call.superseded") continue;
      if (seen.has(event.call.id)) continue;
      seen.add(event.call.id);
      // The row somebody is listening to stays, dimmed and labelled; every other
      // demoted copy simply goes, so the operator is not shown one call twice.
      if (superseded.has(event.call.id) && event.call.id !== currentId) continue;
      at.set(event.call.id, result.length);
      result.push(event.call);
    }
    if (backfillItems !== undefined) {
      let added = false;
      for (const call of backfillItems) {
        if (seen.has(call.id)) {
          // The stream's copy stands unless the archive's is newer — a transcript or an
          // edit that landed while the page was away, whose event never reached us.
          const position = at.get(call.id);
          const held = position === undefined ? undefined : result[position];
          if (position !== undefined && held !== undefined && isOlderCopy(held, call))
            result[position] = call;
          continue;
        }
        if (superseded.has(call.id) && call.id !== currentId) continue;
        seen.add(call.id);
        result.push(call);
        added = true;
      }
      // Only pay for the sort when the archive actually contributed something the
      // stream had not already delivered.
      if (added) result.sort(compareNewestFirst);
    }
    return result;
  }, [feed.events, backfillItems, superseded, currentId]);

  // Hand every arrival to the player, which decides whether to queue or play it.
  // Only a genuinely new head counts: a backfill after a suspension re-sorts the list
  // without anything having *arrived*, and must not auto-play a call from ten minutes
  // ago as though it had just come over the air.
  const announcedId = useRef<number | null>(null);
  const streamedIds = useMemo(() => {
    const ids = new Set<number>();
    for (const event of feed.events) if ("call" in event) ids.add(event.call.id);
    return ids;
  }, [feed.events]);
  // A call the stream has just delivered keeps an amber key-up edge for a few seconds.
  // The ids already in the buffer when the page opened are history, not arrivals.
  const { freshIds, mark: markFresh } = useFreshIds();
  const heardRef = useRef<Set<number> | null>(null);
  useEffect(() => {
    if (heardRef.current === null) {
      heardRef.current = new Set(streamedIds);
      return;
    }
    const heard = heardRef.current;
    const arrived = [...streamedIds].filter((id) => !heard.has(id));
    for (const id of arrived) heard.add(id);
    markFresh(arrived);
  }, [streamedIds, markFresh]);

  useEffect(() => {
    const newest = calls[0];
    if (!newest || newest.id === announcedId.current) return;
    announcedId.current = newest.id;
    // A head the archive supplied (the arrival or resume backfill) is history, not news.
    if (!streamedIds.has(newest.id)) return;
    liveArrived(newest);
  }, [calls, streamedIds, liveArrived]);

  const seedItems = activeSeed.isPlaceholderData ? undefined : activeSeed.data?.items;
  const active = useMemo(() => {
    const cutoff = Date.now() - ACTIVE_WINDOW_MS;
    const byTalkgroup = new Map<string, { radio: string; at: string }[]>();
    // Newest first, so each radio keeps the time it was *last* heard.
    const seen = new Set(calls.map((call) => call.id));
    const all = [...calls, ...(seedItems ?? []).filter((call) => !seen.has(call.id))].sort(
      compareNewestFirst,
    );
    for (const call of all) {
      if (Date.parse(call.started_at) < cutoff) continue;
      const key = callTalkgroupLabel(call);
      const list = byTalkgroup.get(key) ?? [];
      if (!list.some((entry) => entry.radio === (call.radio_label ?? "unresolved"))) {
        list.push({ radio: call.radio_label ?? "unresolved", at: call.started_at });
      }
      byTalkgroup.set(key, list);
    }
    return [...byTalkgroup.entries()].sort((a, b) => b[1].length - a[1].length);
  }, [calls, seedItems]);
  const chipsLabelId = useId();

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Live"
        description="Every call as it is recorded. Turn on auto-play to leave it running like a scanner."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            {/* The feed's state is the header's pill; a second one here said "Live" twice. */}
            <Switch
              checked={live}
              onCheckedChange={setLive}
              label="Follow new calls"
              tip={HELP["live.toggle"]}
            />
            <Switch
              checked={autoPlayNew}
              onCheckedChange={setAutoPlayNew}
              label="Auto-play new calls"
              tip={HELP["live.autoplay"]}
            />
            <Button
              tip={paused ? HELP["live.resume"] : HELP["live.pause"]}
              size="sm"
              active={paused}
              onClick={() => setPaused(!paused)}
            >
              {paused ? <Play size={14} aria-hidden="true" /> : <Pause size={14} aria-hidden="true" />}
              {paused ? "Resume" : "Pause"}
            </Button>
            <Button tip={HELP["live.clear"]} size="sm" variant="ghost" onClick={feed.clear}>
              <Trash2 size={14} aria-hidden="true" /> Clear
            </Button>
          </div>
        }
      />

      <fieldset
        aria-labelledby={chipsLabelId}
        // A fieldset for the group's label; its UA border, margin and min-width reset.
        className="m-0 flex min-w-0 flex-wrap items-center gap-1.5 border-0 px-4 pb-2 pt-0 md:px-6"
      >
        <span id={chipsLabelId} className="mr-1 text-xs font-medium text-ink-2">
          Busiest channels:
        </span>
        {(talkgroups.data?.items ?? [])
          .slice()
          .sort((a, b) => b.call_count - a.call_count)
          .slice(0, 12)
          .map((talkgroup) => (
            <Chip
              key={talkgroup.id}
              label={talkgroupLabel(talkgroup)}
              tip={HELP["live.filterChip"]}
              // `?talkgroup=` is the on-air tgid, never the row's primary key.
              active={filters.talkgroup.includes(talkgroup.tgid)}
              onClick={() =>
                patch({
                  talkgroup: filters.talkgroup.includes(talkgroup.tgid)
                    ? filters.talkgroup.filter((tgid) => tgid !== talkgroup.tgid)
                    : [...filters.talkgroup, talkgroup.tgid],
                })
              }
            />
          ))}
      </fieldset>

      {/* `minmax(0, 1fr)` and `min-w-0`: a grid track's minimum is its content's, so a long
          transcript row pushed "Who's active" off the right edge at 1440 px. */}
      <div className="grid min-h-0 flex-1 gap-4 p-4 md:p-6 lg:grid-cols-[minmax(0,1fr)_18rem]">
        <Card className="flex min-h-0 min-w-0 flex-col">
          <CardHeader
            title="Feed"
            hint={HELP["live.feed"]}
            actions={
              <Tip label={HELP["live.quiet"]}>
                <span tabIndex={0} className="text-[11px] tabular-nums text-muted">
                  {feed.lastEventAt
                    ? `last ${formatRelative(new Date(feed.lastEventAt).toISOString())}`
                    : "waiting…"}
                </span>
              </Tip>
            }
          />
          {calls.length === 0 ? (
            <EmptyState
              title={live ? "Listening…" : "The feed is off"}
              hint={
                live
                  ? "Nothing has come through since you opened this page. Calls appear here as they are recorded."
                  : "Turn on Follow new calls to start receiving calls."
              }
              icon={<Radio size={28} />}
            />
          ) : (
            <ul className="min-h-0 flex-1 divide-y divide-line overflow-y-auto scrollbar-thin">
              {calls.map((call) => (
                <li key={call.id}>
                  <button
                    type="button"
                    onClick={() => select(call)}
                    className={cn(
                      "key-edge flex w-full items-center gap-3 px-4 py-2 text-left text-sm hover:bg-surface-2",
                      call.emergency === true && "bg-critical-soft",
                      call.id === currentId && "bg-accent-soft",
                      // Only ever the row in the player: every other demoted copy is
                      // already gone from `calls`.
                      superseded.has(call.id) && "opacity-60",
                    )}
                    style={{ "--edge": channelEdgeColor(call.category, theme) } as CSSProperties}
                    data-playing={call.id === playingId ? "true" : undefined}
                    data-fresh={freshIds.has(call.id) ? "true" : undefined}
                    data-emergency={call.emergency === true ? "true" : undefined}
                    data-superseded={superseded.has(call.id) ? "true" : undefined}
                  >
                    <span className="w-16 shrink-0 tabular-nums text-xs text-ink-2">
                      {formatClock(call.started_at)}
                    </span>
                    <span className="w-44 shrink-0 truncate font-condensed text-[15px] font-semibold text-ink">
                      {call.patch_label ?? callTalkgroupLabel(call)}
                    </span>
                    <span className="w-24 shrink-0 truncate text-xs tabular-nums text-ink-2">
                      {call.radio_label ?? "—"}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-ink">
                      {call.transcript_text ?? (
                        // The reason in words ("encrypted", "no speech"), never the raw status.
                        <span className="italic text-muted">{noTranscriptReason(call)}</span>
                      )}
                    </span>
                    {superseded.has(call.id) ? (
                      <span className="shrink-0 rounded bg-surface-2 px-1 text-[11px] text-muted">
                        superseded
                      </span>
                    ) : null}
                    <QualityDot quality={call.transcript_quality} />
                    <span className="w-12 shrink-0 text-right text-xs tabular-nums text-muted">
                      {formatDuration(call.duration_s)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card className="flex min-h-0 min-w-0 flex-col">
          <CardHeader title="Who's active" hint={HELP["live.whoActive"]} />
          {active.length === 0 ? (
            <EmptyState title="Nobody yet" hint="Radios heard in the last ten minutes appear here." />
          ) : (
            // The radio chips are not tab stops (they only carry a tooltip), so the list
            // itself is: on a phone it scrolls, and a keyboard has to reach it to scroll it.
            <ul
              className="min-h-0 flex-1 divide-y divide-line overflow-y-auto scrollbar-thin"
              aria-label="Who's active"
              tabIndex={0}
            >
              {active.map(([alias, radios]) => (
                <li key={alias} className="px-3 py-2">
                  <p className="truncate text-xs font-semibold text-ink">{alias}</p>
                  <ul className="mt-1 flex flex-wrap gap-1">
                    {radios.map((entry) => (
                      <li key={entry.radio}>
                        <Tip label={`${HELP["live.whoActive.radio"]} — last heard ${formatClock(entry.at)}`}>
                          <span
                            tabIndex={0}
                            className="inline-block rounded-full border border-line px-1.5 py-0.5 text-[11px] text-ink-2"
                          >
                            {entry.radio}
                          </span>
                        </Tip>
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
          )}
          <div className="border-t border-line p-3">
            <Link to="/radios" className="text-xs text-accent-ink hover:underline">
              Full radio directory
            </Link>
          </div>
        </Card>
      </div>
    </div>
  );
}
