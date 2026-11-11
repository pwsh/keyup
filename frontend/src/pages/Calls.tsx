import { LayoutGrid, PanelLeft, PanelRight, Rows3 } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useCallFacets, useCalls, useCallsScroll, useCurrentUser, useUpdateCall } from "../api/hooks";
import type { Call, TranscriptQuality } from "../api/types";
import {
  CallCards,
  type CallColumnKey,
  CallsTable,
  DEFAULT_CALL_COLUMNS,
  normalizeColumns,
} from "../components/calls/CallsTable";
import { MoreMenu, ViewMenu } from "../components/calls/ColumnsMenu";
import { ExportMenu } from "../components/calls/ExportMenu";
import { FacetsPanel } from "../components/calls/FacetsPanel";
import { FeedDialog } from "../components/calls/FeedDialog";
import { FilterRail } from "../components/calls/FilterRail";
import { LiveIndicator, NewCallsPill } from "../components/calls/LiveControls";
import { LIVE_LIST_CAP } from "../components/calls/liveCalls";
import { useLiveCalls } from "../components/calls/useLiveCalls";
import { PageHeader } from "../components/layout/PageHeader";
import { Button } from "../components/ui/Button";
import { Chip } from "../components/ui/Chip";
import { Dialog } from "../components/ui/Dialog";
import { Select } from "../components/ui/Select";
import { SkeletonRows } from "../components/ui/Skeleton";
import { EmptyState, ErrorState } from "../components/ui/States";
import { Switch } from "../components/ui/Switch";
import { Tip } from "../components/ui/Tooltip";
import { HELP } from "../help/help";
import { formatNumber } from "../lib/format";
import { useLocalStorage } from "../lib/useLocalStorage";
import { useMediaQuery } from "../lib/useMediaQuery";
import { DEFAULT_SORT, useApiParams, useCallFilters } from "../state/filters";
import { FACETS_PANEL, FILTER_RAIL_PANEL, usePanelOpen } from "../state/panels";
import { usePlayerStore } from "../state/player";
import { toast } from "../state/toasts";

/** The compact quality control's options, each a preset over the `quality` filter array. */
const QUALITY_OPTIONS = [
  { value: "all", label: "All quality" },
  { value: "good", label: "Good only" },
  { value: "suspect", label: "Suspect only" },
  { value: "noise", label: "Noise only" },
  { value: "hide-noise", label: "Hide noise" },
];

const QUALITY_SETS: Record<string, TranscriptQuality[]> = {
  all: [],
  good: ["good"],
  suspect: ["suspect"],
  noise: ["noise"],
  "hide-noise": ["good", "suspect"],
};

/** Which option the current `quality` array matches; combinations the rail can build
 *  that this control cannot express fall back to "All". */
function qualitySelection(quality: TranscriptQuality[]): string {
  const key = [...quality].sort().join(",");
  if (key === "") return "all";
  if (key === "good") return "good";
  if (key === "suspect") return "suspect";
  if (key === "noise") return "noise";
  if (key === "good,suspect") return "hide-noise";
  return "all";
}

/**
 * Rows per request. The first page is what the reader waits for, so it is small; the
 * rest arrive a page at a time as they scroll, up to the list's cap.
 */
const PAGE_SIZE = 100;

/** How close to the end of the list, in pixels, the next page is asked for. */
const LOAD_MORE_PX = 800;

const COLUMNS_KEY = "keyup-calls-columns";
const TRANSCRIPT_ROW_KEY = "keyup-calls-transcript-row";

/**
 * The default view: filters on the left, results in the middle, facet counts on
 * the right, and the player pinned to the foot of the window. Selecting a row
 * loads it into the player rather than navigating, so a shift can walk a list
 * without losing their place.
 */
export function Calls() {
  const { filters, patch, reset, activeCount, query } = useCallFilters();
  const desktop = useMediaQuery("(min-width: 1024px)");
  // The breakdown is a third column: below this the rail, the table and it together
  // leave the transcript column too narrow to read, so it stays closed.
  const wide = useMediaQuery("(min-width: 1600px)");
  const [railOpen, setRailOpen] = usePanelOpen(FILTER_RAIL_PANEL);
  const [facetsOpen, setFacetsOpen] = usePanelOpen(FACETS_PANEL);
  const { canWrite } = useCurrentUser();
  const updateCall = useUpdateCall();

  // Saved before the Emg/Enc/Tone columns merged into one, a choice can name keys that no
  // longer exist; `normalizeColumns` maps those onto `flags` and drops anything else.
  const [savedColumns, setColumns] = useLocalStorage<string[]>(COLUMNS_KEY, DEFAULT_CALL_COLUMNS);
  const columns: CallColumnKey[] = useMemo(() => normalizeColumns(savedColumns), [savedColumns]);
  // The transcript gets its own row by default once there is room for the table beside it
  // (1280 px and up): inline, between the rail and the fixed columns, it was ~130 px wide.
  // A reader who has chosen either way keeps their choice at every width.
  const roomy = useMediaQuery("(min-width: 1280px)");
  const [savedTranscriptRow, setTranscriptRow] = useLocalStorage<boolean | null>(TRANSCRIPT_ROW_KEY, null);
  const transcriptRow = savedTranscriptRow ?? roomy;

  const { params, refresh: refreshWindow } = useApiParams(filters, { page_size: PAGE_SIZE });
  const calls = useCallsScroll(params);
  // Only asked for while the breakdown is actually on screen: it is the slowest query the
  // page makes (tens of seconds cold), and below 1600 px or with the column closed nobody
  // would see the answer.
  const facetsShown = wide && facetsOpen;
  const facets = useCallFacets(params, facetsShown);

  // How many calls the "Audio only" toggle is holding back — the same filters, but only
  // the metadata-only rows. Counted with a one-row page, and only while the toggle is on.
  const hiddenParams = useMemo(() => ({ ...params, has_audio: false, page_size: 1 }), [params]);
  const hidden = useCalls(hiddenParams, filters.audio_only);
  const hiddenCount = filters.audio_only ? (hidden.data?.total ?? 0) : 0;

  const pages = calls.data?.pages;
  const page = useMemo(() => pages?.[0]?.items ?? [], [pages]);
  const more = useMemo(() => (pages ?? []).slice(1).flatMap((next) => next.items), [pages]);
  // The count is the first page's: a cursor page (the next page of a time sort) carries none.
  // Null until it is known, so a page still loading (or one that failed) never claims "0 calls".
  const total = pages?.[0]?.total ?? null;
  const selectedId = usePlayerStore((state) => state.current?.id ?? null);
  // The row whose key-up edge glows: only while the player is actually sounding it.
  const playingId = usePlayerStore((state) => (state.playing ? (state.current?.id ?? null) : null));
  const setQueue = usePlayerStore((state) => state.setQueue);
  const select = usePlayerStore((state) => state.select);
  const refresh = usePlayerStore((state) => state.refresh);

  // Live only applies to the view live can honestly insert into: the newest-first
  // one. Under any other sort a call recorded a second ago does not belong at the
  // top, so the feed is held and the indicator says so.
  const live = useLiveCalls({
    base: page,
    more,
    params,
    // The filters, not the params: a rolling window re-read on resume is the same
    // question, and its answer is folded in like any refetch rather than replacing the list.
    questionKey: query,
    eligible: filters.sort === DEFAULT_SORT,
    selectedId,
  });
  const items = live.items;

  // The next page, once the reader nears the end of what is loaded. One scroll listener on
  // the list's own container covers the table and the cards alike; it stops at the live
  // list's cap, which is where the list would start dropping rows off the bottom anyway.
  // Not while the previous filter's pages stand in for the new one's: their cursor
  // belongs to a different question.
  const { hasNextPage, isFetchingNextPage, isPlaceholderData, fetchNextPage } = calls;
  const canLoadMore =
    hasNextPage && !isFetchingNextPage && !isPlaceholderData && items.length < LIVE_LIST_CAP;
  const scrollRef = live.scrollRef;
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-attach when the list container is swapped (table <-> cards)
  useEffect(() => {
    const element = scrollRef.current;
    if (element === null || !canLoadMore) return;
    const check = () => {
      // A container with no height has not been laid out (or is hidden): it is not "at the
      // end", and treating it so would pull every page down on mount.
      if (element.clientHeight === 0) return;
      if (element.scrollHeight - element.scrollTop - element.clientHeight < LOAD_MORE_PX)
        void fetchNextPage();
    };
    check();
    element.addEventListener("scroll", check, { passive: true });
    return () => element.removeEventListener("scroll", check);
  }, [scrollRef, canLoadMore, fetchNextPage, desktop, items.length]);

  // The player's previous/next walk whatever this page is showing.
  useEffect(() => {
    setQueue(items);
  }, [items, setQueue]);

  // The feed has just come back from a suspension — the phone was locked while a call
  // played, the tab was in the background. A stream replays nothing, so everything
  // recorded in the meantime exists only on the server: ask for the page again, and
  // `useLiveCalls` folds the answer in like any other arrival, without moving the list.
  // A rolling window is measured from now again first; when that moved its start, the
  // new query key fetches on its own and only an unchanged one needs the refetch.
  const resumedAt = live.resumedAt;
  useEffect(() => {
    if (resumedAt === null) return;
    if (!refreshWindow()) void calls.refetch();
  }, [resumedAt, refreshWindow, calls.refetch]);

  // A transcript landing on the call in the player is the same event as a transcript
  // landing on its row: hand the fresher copy over so the player is not left holding
  // the version that arrived before the text did.
  useEffect(() => {
    if (selectedId === null) return;
    const current = items.find((call) => call.id === selectedId);
    if (current) refresh(current);
  }, [items, selectedId, refresh]);

  // The export selection. It lives here rather than in the table because the table is
  // virtualized — every row that scrolls out of view is unmounted — and because the
  // export menu, which is not inside the table at all, is what finally reads it.
  const [picked, setPicked] = useState<ReadonlySet<number>>(() => new Set());
  // A tick that no longer matches the filters must not follow the reader around: changing
  // the filters is changing the question, and the answer starts empty.
  // Keyed on the filters rather than the params, so a rolling window moving on after a
  // suspension does not throw the reader's ticks away.
  // biome-ignore lint/correctness/useExhaustiveDependencies: clear on the filter set only
  useEffect(() => {
    setPicked(new Set());
  }, [query]);
  // The row callbacks are stable, so a live update re-renders only the rows whose call
  // changed (`CallRow` is `memo`). `mutate` is stable across renders in TanStack Query.
  const mutateCall = updateCall.mutate;
  const toggleStar = useCallback(
    (call: Call) =>
      mutateCall(
        { id: call.id, patch: { starred: !call.starred } },
        { onError: () => toast("Could not save the star", { tone: "error" }) },
      ),
    [mutateCall],
  );
  const checkOne = useCallback(
    (call: Call, on: boolean) =>
      setPicked((current) => {
        const next = new Set(current);
        if (on) next.add(call.id);
        else next.delete(call.id);
        return next;
      }),
    [],
  );

  // Everything the toolbar offers besides Filters, the count and Live. On a phone these
  // go behind "…" so the toolbar is one row; from `lg` they sit in the toolbar itself.
  const audioOnly = (
    <Switch
      checked={filters.audio_only}
      onCheckedChange={(next) => patch({ audio_only: next })}
      label="Audio only"
      tip={HELP["calls.toolbar.audioOnly"]}
    />
  );
  const hiddenNote =
    hiddenCount > 0 ? (
      <Tip label={HELP["calls.toolbar.audioOnly"]}>
        <button
          type="button"
          onClick={() => patch({ audio_only: false })}
          className="rounded text-[11px] tabular-nums text-muted hover:text-ink"
        >
          {formatNumber(hiddenCount)} metadata-only hidden
        </button>
      </Tip>
    ) : null;
  const qualitySelect = (
    <Select
      value={qualitySelection(filters.quality)}
      onValueChange={(value) => patch({ quality: QUALITY_SETS[value] ?? [] })}
      options={QUALITY_OPTIONS}
      tip={HELP["calls.toolbar.quality"]}
      ariaLabel="Filter by transcript quality"
      size="sm"
    />
  );
  const exportMenu = <ExportMenu filters={params} visible={items} selected={picked} />;
  // Creating a feed is a write; a guest is not shown a button they cannot use.
  const feedButton = canWrite ? <FeedDialog filters={params} canWrite={canWrite} /> : null;
  // No green "Live" over a list that failed to load: nothing is being kept current.
  const liveIndicator = calls.error ? null : (
    <LiveIndicator
      status={live.status}
      paused={live.paused}
      onPausedChange={live.setPaused}
      reason={live.pausedReason}
    />
  );
  const countText = (
    <Tip label={HELP["calls.table.count"]}>
      <p tabIndex={0} className="truncate text-sm tabular-nums text-ink-2">
        {total === null ? (
          calls.error ? (
            "Calls unavailable"
          ) : (
            "Loading calls…"
          )
        ) : (
          <>
            <span className="font-semibold text-ink">{formatNumber(total)}</span> calls
          </>
        )}
      </p>
    </Tip>
  );

  const toolbar = desktop ? (
    <div
      data-testid="calls-toolbar"
      className="flex shrink-0 flex-wrap items-center gap-x-2 gap-y-1.5 border-b border-line px-3 py-2"
    >
      <Button
        tip={railOpen ? "Hide the filter rail" : HELP["calls.filter.open"]}
        size="iconSm"
        variant="ghost"
        active={railOpen}
        aria-label={railOpen ? "Hide the filters" : "Show the filters"}
        onClick={() => setRailOpen(!railOpen)}
      >
        <PanelLeft size={15} aria-hidden="true" />
      </Button>
      {countText}
      {picked.size > 0 ? (
        <Chip
          label={`${formatNumber(picked.size)} selected`}
          tip={HELP["calls.export.clearSelection"]}
          active
          onClick={() => setPicked(new Set())}
        />
      ) : null}
      {audioOnly}
      {hiddenNote}
      {qualitySelect}
      <div className="ml-auto flex items-center gap-1.5">
        {liveIndicator}
        <ViewMenu
          columns={columns}
          onColumnsChange={setColumns}
          transcriptRow={transcriptRow}
          onTranscriptRowChange={setTranscriptRow}
        />
        {exportMenu}
        {feedButton}
        {wide ? (
          <Button
            tip={facetsOpen ? "Hide the breakdown" : "Show the breakdown of these results"}
            size="iconSm"
            variant="ghost"
            active={facetsOpen}
            aria-label={facetsOpen ? "Hide the breakdown" : "Show the breakdown"}
            onClick={() => setFacetsOpen(!facetsOpen)}
          >
            <PanelRight size={15} aria-hidden="true" />
          </Button>
        ) : null}
      </div>
    </div>
  ) : (
    // One row on a phone: Filters, the count, Live, and "…" for the rest. It used to
    // wrap to three rows, which with the header put the first call 218 px down.
    <div
      data-testid="calls-toolbar"
      className="flex shrink-0 items-center gap-2 border-b border-line px-3 py-1.5"
    >
      <Button
        tip={HELP["calls.filter.open"]}
        size="sm"
        active={activeCount > 0}
        onClick={() => setRailOpen(true)}
        className="shrink-0"
      >
        <PanelLeft size={14} aria-hidden="true" />
        {/* sr-only rather than hidden at 320 px, so the button keeps its name. Named by
            its own text ("Filters 2 active"), not an aria-label that differs from it. */}
        <span className="max-[359px]:sr-only">Filters</span>
        {activeCount ? (
          <span className="rounded-full bg-accent-fill px-1.5 text-[11px] font-semibold text-white">
            {activeCount}
            <span className="sr-only"> active</span>
          </span>
        ) : null}
      </Button>
      <div className="min-w-0">{countText}</div>
      <div className="ml-auto flex shrink-0 items-center gap-1">
        {liveIndicator}
        <MoreMenu>
          {audioOnly}
          {hiddenNote}
          {qualitySelect}
          <div className="flex flex-wrap items-center gap-2">
            {exportMenu}
            {feedButton}
          </div>
        </MoreMenu>
      </div>
    </div>
  );

  const results = (
    // `min-w-0`: without it this column grew to its widest row, and at 1440 px with the
    // sidebar and rail open that pushed Export and every row's actions past the window.
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      {toolbar}
      {/* The "Skip to calls" target: always here, whether it holds rows, a skeleton or
          an empty state, so the link never points at nothing. */}
      <div id="calls-list" tabIndex={-1} className="flex min-h-0 flex-1 flex-col focus:outline-none">
        {calls.error ? (
          <ErrorState error={calls.error} onRetry={() => void calls.refetch()} />
        ) : calls.isLoading && !calls.data ? (
          <SkeletonRows rows={10} label="Loading calls" className="px-4 py-2" />
        ) : items.length === 0 ? (
          <EmptyState
            title="No calls match these filters"
            hint="Widen the date range, clear a talkgroup or two, or drop the text query."
            icon={<LayoutGrid size={28} />}
            action={
              <Button tip={HELP["calls.filter.reset"]} size="sm" onClick={reset} className="mt-2">
                Clear the filters
              </Button>
            }
          />
        ) : desktop ? (
          // Positioned, because the "N new calls" pill floats over the top of the rows
          // rather than taking a strip of height that would itself move the list.
          <div className="relative flex min-h-0 flex-1 flex-col">
            <NewCallsPill count={live.newCount} onClick={live.jumpToTop} />
            <CallsTable
              calls={items}
              selectedId={selectedId}
              onSelect={select}
              sort={filters.sort}
              onSortChange={(sort) => patch({ sort })}
              query={filters.q}
              canWrite={canWrite}
              onToggleStar={toggleStar}
              columns={columns}
              transcriptRow={transcriptRow}
              checked={picked}
              onCheck={checkOne}
              onCheckAll={(on) =>
                setPicked((current) => {
                  // Only the loaded page is ticked or cleared: "all" cannot mean rows nobody
                  // has seen, and the export of everything matching is its own menu entry.
                  const next = new Set(current);
                  for (const call of items) {
                    if (on) next.add(call.id);
                    else next.delete(call.id);
                  }
                  return next;
                })
              }
              freshIds={live.freshIds}
              playingId={playingId}
              supersededIds={live.supersededIds}
              containerRef={live.scrollRef}
              className="min-h-0 flex-1"
            />
          </div>
        ) : (
          <div className="relative flex min-h-0 flex-1 flex-col">
            <NewCallsPill count={live.newCount} onClick={live.jumpToTop} />
            <div ref={live.scrollRef} className="min-h-0 flex-1 overflow-y-auto scrollbar-thin">
              <CallCards
                calls={items}
                selectedId={selectedId}
                playingId={playingId}
                freshIds={live.freshIds}
                onSelect={select}
                query={filters.q}
              />
            </div>
          </div>
        )}
      </div>
    </div>
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* First stop on the page: past the header, the toolbar and (on a desk) the whole
          filter rail, which is dozens of Tabs before the first call. */}
      <a
        href="#calls-list"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-surface focus:px-3 focus:py-2 focus:text-sm focus:text-ink"
      >
        Skip to calls
      </a>
      <PageHeader
        title="Calls"
        description="Every recorded transmission, filtered however you like. Select a row to play it; the transcript and the exchange around it are on the call's own page."
        actions={
          <Tip label={HELP["calls.view.cards"]}>
            <span tabIndex={0} className="hidden items-center gap-1 text-[11px] text-muted md:inline-flex">
              <Rows3 size={12} aria-hidden="true" />
              {desktop ? "Table" : "Cards"}
            </span>
          </Tip>
        }
      />

      <div className="flex min-h-0 flex-1">
        {desktop && railOpen ? (
          <FilterRail
            filters={filters}
            patch={patch}
            reset={reset}
            activeCount={activeCount}
            className="w-72 shrink-0 border-r border-line"
          />
        ) : null}

        {results}

        {facetsShown ? (
          <FacetsPanel
            facets={facets.data}
            isLoading={facets.isLoading}
            selectedTalkgroups={filters.talkgroup}
            onToggleTalkgroup={(id) =>
              patch({
                talkgroup: filters.talkgroup.includes(id)
                  ? filters.talkgroup.filter((value) => value !== id)
                  : [...filters.talkgroup, id],
              })
            }
            onClose={() => setFacetsOpen(false)}
            className="w-64 shrink-0 border-l border-line"
          />
        ) : null}
      </div>

      {/* Below `lg` the rail becomes a sheet, so the results never share the width. */}
      {!desktop ? (
        <Dialog
          open={railOpen}
          onOpenChange={setRailOpen}
          title="Filters"
          description="Every filter is kept in the address bar, so this view is one shareable link."
          // The sheet has no padding of its own so the rail's dividers run edge to edge; the
          // dialog's title row (its first child) gets it back here, and the rail drops its own
          // header so there is one title and one close button.
          className="max-h-[85vh] p-0 [&>div:first-child]:mb-0 [&>div:first-child]:border-b [&>div:first-child]:border-line [&>div:first-child]:px-4 [&>div:first-child]:pb-3 [&>div:first-child]:pt-4"
        >
          <FilterRail
            filters={filters}
            patch={patch}
            reset={reset}
            activeCount={activeCount}
            onClose={() => setRailOpen(false)}
            embedded
            className="max-h-[70vh]"
          />
        </Dialog>
      ) : null}
    </div>
  );
}
