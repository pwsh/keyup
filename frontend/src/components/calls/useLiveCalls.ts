/**
 * The Calls page's live list.
 *
 * `useLiveFeed` already owns the socket (`WS /api/live`, filter as the first frame,
 * backoff on a drop). This hook owns what the *page* does with it, which is mostly a
 * promise not to move anything:
 *
 * - a call that is selected is never unselected, never re-inserted, and never shifts
 *   on screen — arrivals above it are compensated for by adding the height they took
 *   to the container's `scrollTop`, so the selected row keeps the same pixel;
 * - a reader scrolled away from the top gets the same compensation plus a
 *   "N new calls" pill, so the archive can grow without stealing their place;
 * - a reader at the top with nothing selected gets the plain behaviour: the new rows
 *   push the list down, briefly highlighted.
 *
 * Arrivals are deduplicated by call id, so `call.transcribed` for a row already on
 * screen updates that row where it stands (`mergeCalls`) rather than adding a second
 * copy — that is how a transcript appears without a refresh.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useLiveFeed } from "../../api/live";
import type { Call, CallFilterParams, LiveEvent } from "../../api/types";
import { useLocalStorage } from "../../lib/useLocalStorage";
import { isOlderCopy, LIVE_LIST_CAP, mergeCalls } from "./liveCalls";
import { useFreshIds } from "./useFreshIds";

/** Where the pause toggle is remembered; a reader who turned it off meant it. */
export const LIVE_PAUSE_KEY = "keyup-calls-live-paused";

/** Within this many pixels of the top counts as "at the top". */
const TOP_EPSILON = 4;

const EMPTY_IDS: ReadonlySet<number> = new Set<number>();
const EMPTY_CALLS: Call[] = [];

/** What the indicator says: the feed is running, coming back, or deliberately off. */
export type LiveStatus = "live" | "reconnecting" | "paused";

export interface LiveCallsOptions {
  /** The page the paginated query returned — the list live rows are folded into. */
  base: Call[];
  /**
   * The rows of the pages after the first, loaded as the reader scrolls towards the end.
   * They belong *below* everything the list holds: appended, deduplicated by id, and never
   * counted as new, highlighted or used to anchor the scroll — the reader asked for them.
   */
  more?: Call[];
  /** The active filters, sent to the socket verbatim so the feed matches the page. */
  params: CallFilterParams;
  /**
   * What identifies the *question*, when that is not simply `params`. A change resets the
   * list to the next page the query returns; a change of `params` alone is then treated
   * like a refetch and folded in. The Calls page passes its serialised filters, so a
   * rolling window re-read from the clock after a suspension does not move the list.
   */
  questionKey?: string;
  /**
   * Whether live mode applies at all: the first page with the default sort. On any
   * other view the indicator says paused and explains why, rather than quietly
   * inserting rows into an order they do not belong in.
   */
  eligible: boolean;
  /** The call in the player. It is never dropped, and never moved. */
  selectedId: number | null;
}

export interface LiveCalls {
  /** What the table should render: the page, plus everything live has added. */
  items: Call[];
  /** Arrivals the reader has not seen yet, because they were not at the top. */
  newCount: number;
  status: LiveStatus;
  /** Why the feed is paused when the reader did not pause it, else null. */
  pausedReason: string | null;
  paused: boolean;
  setPaused: (next: boolean) => void;
  /** The pill's action: back to the top, counter cleared. */
  jumpToTop: () => void;
  /** Hand this to the table so the hook can compensate its scroll position. */
  scrollRef: React.RefObject<HTMLDivElement | null>;
  /** Ids to flash for a moment because they have just arrived. */
  freshIds: ReadonlySet<number>;
  /**
   * Calls reconciliation has demoted since this list was built. They are taken out of
   * `items` as the event arrives — except the one in the player, which is kept where
   * it is and left in this set so the table can dim it and say why. It falls off at
   * the next refresh, when the query no longer returns it.
   */
  supersededIds: ReadonlySet<number>;
  /**
   * Bumped when the feed comes back from a suspended page — a phone that was locked
   * while a call played, a tab that was in the background. Nothing was buffered while
   * we were away, so the page refetches its window rather than showing a hole.
   */
  resumedAt: number | null;
}

/** The call events carry a call; `ingest.status` and `call.superseded` do not. */
const callOf = (event: LiveEvent): Call | null =>
  event.type === "ingest.status" || event.type === "call.superseded" ? null : event.call;

export function useLiveCalls({
  base,
  more = EMPTY_CALLS,
  params,
  questionKey,
  eligible,
  selectedId,
}: LiveCallsOptions): LiveCalls {
  const [paused, setPaused] = useLocalStorage<boolean>(LIVE_PAUSE_KEY, false);
  const enabled = eligible && !paused;
  const feed = useLiveFeed(params, enabled, questionKey);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [items, setItems] = useState<Call[]>(base);
  const [newCount, setNewCount] = useState(0);
  // Arrivals keep an amber key-up edge for a few seconds each (`useFreshIds`).
  const { freshIds, mark: markFresh, clear: clearFresh } = useFreshIds();
  const [supersededIds, setSupersededIds] = useState<ReadonlySet<number>>(EMPTY_IDS);

  // The list is kept in a ref as well as in state so an arrival can be merged
  // outside the state updater: the updater has to stay pure, and merging is where
  // the decision to anchor the scroll or bump the counter is made.
  const itemsRef = useRef<Call[]>(base);
  const selectedRef = useRef<number | null>(selectedId);
  selectedRef.current = selectedId;
  /** The scroll position to restore once the browser has laid the new rows out. */
  const anchorRef = useRef<{ top: number; height: number } | null>(null);

  const paramsKey = useMemo(() => questionKey ?? JSON.stringify(params), [questionKey, params]);
  const paramsKeyRef = useRef(paramsKey);

  const setList = useCallback((next: Call[]) => {
    itemsRef.current = next;
    setItems(next);
  }, []);

  /**
   * Fold arrivals in, and decide what the reader should see happen.
   *
   * Anchored (something selected, or scrolled off the top) means: remember where the
   * container is *now*, and put it back there after the layout pass — the rows above
   * grew by some number of pixels and `scrollTop` gains exactly that many, so nothing
   * under the reader's eye moves. Otherwise the insertion is allowed to push the list
   * down, which is what "bump the list" means when you are already at the top.
   */
  /**
   * Live arrivals since the filter last changed, kept until the new filter's first page
   * has landed. The socket for the new filter opens at once, but the list is still the old
   * answer until the query returns; replacing it with that page used to throw away
   * everything that had arrived in between — the newest calls, the ones a live view is for.
   */
  const pendingRef = useRef<Call[] | null>(null);

  const applyArrivals = useCallback(
    (arrivals: Call[]) => {
      if (arrivals.length === 0) return;
      const merged = mergeCalls(itemsRef.current, arrivals, {
        cap: LIVE_LIST_CAP,
        protectedId: selectedRef.current,
      });
      if (!merged.changed) return;
      if (merged.inserted.length > 0) {
        const element = scrollRef.current;
        const atTop = element === null || element.scrollTop <= TOP_EPSILON;
        const anchored = selectedRef.current !== null || !atTop;
        if (anchored) {
          if (element !== null) anchorRef.current = { top: element.scrollTop, height: element.scrollHeight };
          setNewCount((count) => count + merged.inserted.length);
        } else {
          markFresh(merged.inserted);
        }
      }
      setList(merged.items);
    },
    [setList, markFresh],
  );

  /**
   * Reconciliation has ruled one of these rows a copy of another.
   *
   * The demoted row is not coming back — a `call.new` for it would be filtered out
   * server-side, and the winner arrives as its own arrival — so it simply leaves the
   * list, with the same scroll compensation an insertion gets so nothing moves under
   * the reader. The one exception is the call in the player: pulling a row out from
   * under somebody who is listening to it is worse than showing a stale one, so it
   * stays, dimmed and labelled, until the next refresh drops it.
   */
  const applySuperseded = useCallback(
    (ids: number[]) => {
      if (ids.length === 0) return;
      const known = new Set(ids.filter((id) => itemsRef.current.some((call) => call.id === id)));
      if (known.size === 0) return;
      const playing = selectedRef.current;
      const drop = new Set(known);
      // Only a row that *stays* is marked; everything else is about to leave the list.
      if (playing !== null && drop.delete(playing)) {
        setSupersededIds((previous) => new Set([...previous, playing]));
      }
      if (drop.size === 0) return;
      const element = scrollRef.current;
      if (element !== null) anchorRef.current = { top: element.scrollTop, height: element.scrollHeight };
      setList(itemsRef.current.filter((call) => !drop.has(call.id)));
    },
    [setList],
  );

  // A new filter set is a new question: the answer starts from the server's page,
  // with no counter and no leftovers from the last one. Otherwise a refetch of the
  // same query is folded in like any other arrival, so a background refresh cannot
  // scroll the reader either.
  // `useCalls` keeps the previous page as placeholder data while the next one loads, so
  // at the moment the params change `base` is still the *old* answer. Taking it as the
  // new list and then folding the real page in as "arrivals" left the old talkgroups'
  // rows sitting under the new filter. So: note that a fresh base is owed, keep showing
  // what is there until it lands, and *replace* the list with the first base that
  // differs from the one shown — a cache hit hands it over in the same render.
  const baseRef = useRef<Call[]>(base);
  const awaitingBaseRef = useRef(false);
  /** The new base, with whatever arrived live while it was being fetched folded over it. */
  const takeBase = useCallback(
    (next: Call[]) => {
      const pending = pendingRef.current;
      pendingRef.current = null;
      if (pending === null || pending.length === 0) {
        setList(next);
        return;
      }
      setList(mergeCalls(next, pending, { cap: LIVE_LIST_CAP, protectedId: selectedRef.current }).items);
    },
    [setList],
  );
  useEffect(() => {
    const baseChanged = baseRef.current !== base;
    baseRef.current = base;
    if (paramsKeyRef.current !== paramsKey) {
      paramsKeyRef.current = paramsKey;
      anchorRef.current = null;
      setNewCount(0);
      clearFresh();
      setSupersededIds(EMPTY_IDS);
      pendingRef.current = [];
      if (baseChanged) {
        awaitingBaseRef.current = false;
        takeBase(base);
      } else {
        awaitingBaseRef.current = true;
      }
      return;
    }
    if (!baseChanged) return;
    if (awaitingBaseRef.current || itemsRef.current.length === 0) {
      awaitingBaseRef.current = false;
      takeBase(base);
      return;
    }
    applyArrivals(base);
  }, [base, paramsKey, applyArrivals, takeBase, clearFresh]);

  // The next page of a scroll landed (or a refetch brought fresher copies of one). Its
  // rows go on the end; a row already held — an arrival that shifted across the page
  // boundary, or the same row refetched — is updated where it stands, never repeated.
  // Declared after the base effect so a refetch of every page folds the first page in
  // before the later ones are appended to it.
  const moreRef = useRef<Call[]>(more);
  useEffect(() => {
    if (moreRef.current === more) return;
    moreRef.current = more;
    if (more.length === 0) return;
    const current = itemsRef.current;
    const index = new Map<number, number>();
    for (const [position, call] of current.entries()) index.set(call.id, position);
    let next: Call[] | null = null;
    for (const call of more) {
      const at = index.get(call.id);
      if (at !== undefined) {
        const held = (next ?? current)[at] as Call;
        if (held === call || isOlderCopy(call, held)) continue;
        next ??= [...current];
        next[at] = call;
        continue;
      }
      next ??= [...current];
      index.set(call.id, next.length);
      next.push(call);
    }
    if (next !== null) setList(next);
  }, [more, setList]);

  // The feed's buffer is newest-first and prepend-only, so everything ahead of the
  // event we handled last time is new. Holding the event itself rather than a count
  // survives the buffer dropping its tail.
  const headRef = useRef<LiveEvent | null>(null);
  useEffect(() => {
    const events = feed.events;
    if (!enabled) {
      // Paused: skip whatever came in, so resuming does not dump a backlog.
      headRef.current = events[0] ?? null;
      return;
    }
    if (events.length === 0) return;
    const seen = headRef.current === null ? -1 : events.indexOf(headRef.current);
    const unseen = seen === -1 ? events : events.slice(0, seen);
    headRef.current = events[0] ?? null;
    if (unseen.length === 0) return;
    const arrivals: Call[] = [];
    const superseded: number[] = [];
    // Oldest first, so a `call.new` and the `call.transcribed` that follows it land
    // in that order and the row ends up carrying the transcript.
    for (let i = unseen.length - 1; i >= 0; i--) {
      const event = unseen[i] as LiveEvent;
      if (event.type === "call.superseded") {
        superseded.push(event.id);
        continue;
      }
      const call = callOf(event);
      if (call !== null) arrivals.push(call);
    }
    // Held for the new filter's first page as well, which is about to replace the list.
    if (awaitingBaseRef.current && pendingRef.current !== null) pendingRef.current.push(...arrivals);
    applyArrivals(arrivals);
    // After the arrivals: the winner of a swap comes through as its own `call.new`, and
    // the loser has to go whether or not that has landed yet.
    applySuperseded(superseded);
  }, [feed.events, enabled, applyArrivals, applySuperseded]);

  // Put the container back where it was, now that the new rows have a height. This
  // runs before the browser paints, so the anchored row never visibly jumps.
  // biome-ignore lint/correctness/useExhaustiveDependencies: `items` is not read here — it is the trigger. The rows changing is precisely the moment the container has to be put back.
  useLayoutEffect(() => {
    const anchor = anchorRef.current;
    anchorRef.current = null;
    const element = scrollRef.current;
    if (anchor === null || element === null) return;
    const grew = element.scrollHeight - anchor.height;
    if (grew !== 0) element.scrollTop = anchor.top + grew;
  }, [items]);

  const jumpToTop = useCallback(() => {
    setNewCount(0);
    scrollRef.current?.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

  const status: LiveStatus = !enabled
    ? "paused"
    : feed.state === "open" || feed.state === "mock"
      ? "live"
      : "reconnecting";

  return {
    items,
    newCount,
    status,
    pausedReason: eligible ? null : "Live updates apply to the newest-first view; sort by time to resume",
    paused,
    setPaused,
    jumpToTop,
    scrollRef,
    freshIds,
    supersededIds,
    resumedAt: feed.resumedAt,
  };
}
