import { Bookmark, Play, Search as SearchIcon, Trash2 } from "lucide-react";
import {
  type CSSProperties,
  type FormEvent,
  type MouseEvent,
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Link, useSearchParams } from "react-router";
import {
  useCallFacets,
  useCurrentUser,
  useDeleteSavedSearch,
  useSavedSearches,
  useSaveSearch,
  useSearchScroll,
  useSuggestions,
} from "../api/hooks";
import { autoWidened, flattenSearchPages, SEARCH_LIST_CAP, SEARCH_PAGE_SIZE } from "../api/searchScroll";
import type { Call, FuzzySearch, SearchHit } from "../api/types";
import { FacetsPanel } from "../components/calls/FacetsPanel";
import { FeedDialog } from "../components/calls/FeedDialog";
import { FeedsPanel } from "../components/calls/FeedsPanel";
import { PageHeader } from "../components/layout/PageHeader";
import { Button } from "../components/ui/Button";
import { Card, CardHeader } from "../components/ui/Card";
import { Chip } from "../components/ui/Chip";
import { Dialog } from "../components/ui/Dialog";
import { inputClass, TextInput } from "../components/ui/Field";
import { QualityDot } from "../components/ui/QualityDot";
import { SkeletonRows } from "../components/ui/Skeleton";
import { EmptyState, ErrorState } from "../components/ui/States";
import { Switch } from "../components/ui/Switch";
import { Tip } from "../components/ui/Tooltip";
import { HELP, useHelpText } from "../help/help";
import { cn } from "../lib/cn";
import { channelEdgeColor } from "../lib/color";
import { callTalkgroupLabel, formatCallTime, formatDuration, formatNumber } from "../lib/format";
import { renderHeadline } from "../lib/highlight";
import {
  type CallFilters,
  type DatePreset,
  PRESET_LABELS,
  SEARCH_DEFAULT_PRESET,
  SEARCH_PRESETS,
  toApiParams,
  useCallFilters,
} from "../state/filters";
import { usePlayerStore } from "../state/player";
import { useTheme } from "../state/theme";
import { toast } from "../state/toasts";

/**
 * Below this many exact matches the page offers to include spelling variants. It only
 * *offers*: the results never change unless the reader says so, because an exact search
 * must keep meaning exactly what it did. Zero matches is the one case the API widens
 * by itself (`fallback`), and the notice then says so.
 */
const FEW_MATCHES = 10;

/** How close to the end of the list, in pixels, the next page is asked for. */
const LOAD_MORE_PX = 800;

const SYNTAX = [
  ['"structure fire"', "the exact phrase, in that order"],
  ["fire -drill", "has 'fire', does not have 'drill'"],
  ["engine OR ladder", "either word will do"],
  ["ten-four", "a hyphenated term is one word"],
];

/** Full-text search across transcripts, notes and aliases. */
export function SearchPage() {
  // An aria-label, outside a tooltip: resolved so it is never the loading placeholder.
  const suggestLabel = useHelpText("search.suggest");
  // A search looks back 30 days unless the URL says otherwise; Calls keeps its 24 hours.
  const { filters, patch, reset, activeCount } = useCallFilters(SEARCH_DEFAULT_PRESET);
  const [searchParams] = useSearchParams();
  const [draft, setDraft] = useState(filters.q);
  // -1 is "nothing highlighted": Enter then runs the query as typed.
  const [activeSuggestion, setActiveSuggestion] = useState(-1);
  const [suggestOpen, setSuggestOpen] = useState(false);
  const suggestions = useSuggestions(draft);
  const options = suggestOpen ? (suggestions.data ?? []) : [];
  const [saveOpen, setSaveOpen] = useState(false);
  const [saveName, setSaveName] = useState("");
  const { canWrite } = useCurrentUser();
  const saved = useSavedSearches();
  const saveSearch = useSaveSearch();
  const deleteSaved = useDeleteSavedSearch();
  const select = usePlayerStore((state) => state.select);
  const setQueue = usePlayerStore((state) => state.setQueue);
  // Only the id, so a tick of the playhead does not re-render the list.
  const playingId = usePlayerStore((state) => (state.playing ? (state.current?.id ?? null) : null));

  const params = useMemo(() => toApiParams(filters, { page_size: SEARCH_PAGE_SIZE }), [filters]);
  const results = useSearchScroll(params, filters.q.trim().length > 0);
  const pages = results.data?.pages;
  // The first page is the question's: whether it widened, and how many calls match.
  const first = results.isPlaceholderData ? undefined : pages?.[0];
  const fuzzy = first?.fuzzy ?? null;
  // When the exact query found nothing and the API widened it by itself, the facets must
  // count the widened set too — otherwise the sidebar says zero beside a page of hits.
  const widenedByItself = autoWidened(first);
  const facetParams = useMemo(
    () => (widenedByItself ? { ...params, fuzzy: true } : params),
    [params, widenedByItself],
  );
  // Separately, so the hits render as soon as they are ranked and the counts follow — an
  // all-time count of a common word takes seconds on a cold cache, the hits do not wait.
  // Same filter set, `q` included, so the numbers are the search's own.
  const facets = useCallFacets(facetParams, filters.q.trim().length > 0);
  const list = useMemo(() => flattenSearchPages(pages), [pages]);
  const hits = list.hits;
  // Stable while the hits are, so a memoised card re-renders only when its own call changes.
  const playHit = useCallback(
    (call: Call) => {
      setQueue(hits.map((item) => item.call));
      select(call);
    },
    [hits, setQueue, select],
  );

  // The next page, once the reader nears the end of what is loaded (as on Calls). Not
  // while the previous question's pages stand in for the new one's.
  const listRef = useRef<HTMLUListElement>(null);
  const { hasNextPage, isFetchingNextPage, isPlaceholderData, fetchNextPage } = results;
  const canLoadMore = hasNextPage && !isFetchingNextPage && !isPlaceholderData;
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-check whenever rows are added
  useEffect(() => {
    const element = listRef.current;
    if (element === null || !canLoadMore) return;
    const check = () => {
      // Not laid out (or hidden): not "at the end", or every page would load on mount.
      if (element.clientHeight === 0) return;
      if (element.scrollHeight - element.scrollTop - element.clientHeight < LOAD_MORE_PX)
        void fetchNextPage();
    };
    check();
    element.addEventListener("scroll", check, { passive: true });
    return () => element.removeEventListener("scroll", check);
  }, [canLoadMore, fetchNextPage, hits.length]);

  const run = (query: string) => {
    setDraft(query);
    setSuggestOpen(false);
    setActiveSuggestion(-1);
    patch({ q: query.trim() });
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    run(options[activeSuggestion]?.value ?? draft);
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Search"
        description="Everything the transcripts, your notes and the aliases say, ranked by how well it matches."
        actions={
          <div className="flex items-center gap-2">
            <Button
              tip={canWrite ? HELP["search.save"] : HELP["app.readOnly"]}
              size="sm"
              disabled={!canWrite || !filters.q}
              onClick={() => {
                setSaveName(filters.q.slice(0, 40));
                setSaveOpen(true);
              }}
            >
              <Bookmark size={14} aria-hidden="true" /> Save this search
            </Button>
            <FeedDialog filters={params} canWrite={canWrite} />
          </div>
        }
      />

      <div className="px-4 md:px-6">
        <form onSubmit={submit} aria-label="Search the archive" className="flex flex-wrap items-center gap-2">
          <Tip label={HELP["search.q"]}>
            {/* A full row of its own on a phone: as one flex item among the button and the
                switch it was squeezed to ~40 px with the Search button over it. */}
            <div className="relative flex min-w-0 shrink grow basis-full items-center sm:basis-0">
              <SearchIcon
                size={15}
                aria-hidden="true"
                className="pointer-events-none absolute left-2.5 text-muted"
              />
              <input
                type="search"
                value={draft}
                onChange={(event) => {
                  setDraft(event.target.value);
                  setSuggestOpen(true);
                  setActiveSuggestion(-1);
                }}
                onFocus={() => setSuggestOpen(true)}
                // A click on an option would otherwise be lost to the blur that
                // closes the list before the click lands.
                onBlur={() => window.setTimeout(() => setSuggestOpen(false), 120)}
                onKeyDown={(event) => {
                  if (!options.length) return;
                  if (event.key === "ArrowDown") {
                    setActiveSuggestion((index) => (index + 1) % options.length);
                  } else if (event.key === "ArrowUp") {
                    setActiveSuggestion((index) => (index <= 0 ? options.length - 1 : index - 1));
                  } else if (event.key === "Escape") {
                    setSuggestOpen(false);
                    setActiveSuggestion(-1);
                  } else {
                    return;
                  }
                  event.preventDefault();
                }}
                placeholder='"structure fire" -drill'
                aria-label="Search query"
                role="combobox"
                aria-expanded={options.length > 0}
                aria-controls="search-suggestions"
                aria-autocomplete="list"
                aria-activedescendant={
                  activeSuggestion >= 0 ? `search-suggestion-${activeSuggestion}` : undefined
                }
                className={`${inputClass} h-10 pl-8 text-base`}
              />
              {options.length ? (
                <ul
                  id="search-suggestions"
                  // Aliases, units and frequent terms from `GET /search/suggest`, so a
                  // reader who half-remembers a channel name can find it by prefix.
                  aria-label={suggestLabel}
                  className="absolute inset-x-0 top-11 z-30 max-h-72 overflow-y-auto rounded-md border border-line bg-surface py-1 shadow-lg scrollbar-thin"
                >
                  {options.map((option, index) => (
                    <li key={`${option.kind}:${option.value}`}>
                      <button
                        type="button"
                        id={`search-suggestion-${index}`}
                        role="option"
                        aria-selected={index === activeSuggestion}
                        onMouseDown={(event) => event.preventDefault()}
                        onClick={() => run(option.value)}
                        className={cn(
                          "flex w-full items-baseline gap-2 px-3 py-1.5 text-left text-sm",
                          index === activeSuggestion ? "bg-accent-soft text-ink" : "text-ink-2",
                        )}
                      >
                        <span className="min-w-0 flex-1 truncate">{option.value}</span>
                        <span className="shrink-0 text-[11px] text-muted">
                          {/* Spelled like what was typed rather than containing it. */}
                          {option.fuzzy ? "≈ " : ""}
                          {option.kind}
                        </span>
                        <span className="shrink-0 tabular-nums text-[11px] text-muted">
                          {formatNumber(option.count)}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          </Tip>
          <Button tip={HELP["search.run"]} type="submit" variant="primary">
            Search
          </Button>
          <Switch
            checked={filters.fuzzy}
            onCheckedChange={(checked) => patch({ fuzzy: checked })}
            label="Spelling variants"
            tip={HELP["search.fuzzy"]}
          />
          {activeCount > 0 ? (
            <Button tip={HELP["calls.filter.reset"]} size="sm" variant="ghost" onClick={reset}>
              Clear filters ({activeCount})
            </Button>
          ) : null}
        </form>

        <RangePicker filters={filters} onChange={(preset) => patch({ preset })} />

        <Tip label={HELP["search.syntax"]}>
          <ul tabIndex={0} className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted">
            {SYNTAX.map(([example, meaning]) => (
              <li key={example}>
                <code className="font-mono text-ink-2">{example}</code> — {meaning}
              </li>
            ))}
          </ul>
        </Tip>
      </div>

      <div className="flex min-h-0 flex-1 gap-4 p-4 md:p-6">
        <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-4">
          {!filters.q ? (
            <>
              <EmptyState
                title="Type something to search"
                hint="Transcripts, your notes and every talkgroup and unit alias are covered."
                icon={<SearchIcon size={28} />}
              />
              <FeedsPanel canWrite={canWrite} />
              <SavedSearches
                saved={saved.data ?? []}
                canWrite={canWrite}
                onDelete={(id) => deleteSaved.mutate(id)}
              />
            </>
          ) : results.error ? (
            <ErrorState error={results.error} onRetry={() => void results.refetch()} />
          ) : results.isLoading ? (
            <SkeletonRows rows={8} label="Loading results" />
          ) : hits.length === 0 ? (
            <>
              {fuzzy?.widened ? (
                <FuzzyNotice q={filters.q} fuzzy={fuzzy} offerWidening={false} onChange={() => {}} />
              ) : null}
              <EmptyState
                title="Nothing matched"
                hint={
                  fuzzy && !fuzzy.widened
                    ? "No close spelling of those words is in the archive either. Try fewer words, or a longer date range."
                    : "Try fewer words, drop a quoted phrase, or pick a longer date range."
                }
              />
            </>
          ) : (
            <>
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <Tip label={HELP["search.results"]}>
                  <p tabIndex={0} className="text-sm text-ink-2">
                    <span className="font-semibold text-ink">
                      {formatNumber(list.total)}
                      {list.totalCapped ? "+" : ""}
                    </span>{" "}
                    matching calls
                  </p>
                </Tip>
                <Tip label={HELP["search.loaded"]}>
                  <p tabIndex={0} className="text-xs tabular-nums text-muted" data-testid="search-loaded">
                    {formatNumber(hits.length)} of {formatNumber(list.total)}
                    {list.totalCapped ? "+" : ""} loaded
                    {isFetchingNextPage ? " — loading more…" : ""}
                  </p>
                </Tip>
              </div>
              <FuzzyNotice
                q={filters.q}
                fuzzy={fuzzy}
                offerWidening={!filters.fuzzy && !fuzzy && list.total < FEW_MATCHES}
                onChange={(checked) => patch({ fuzzy: checked })}
              />
              <ul ref={listRef} className="min-h-0 flex-1 space-y-2 overflow-y-auto scrollbar-thin">
                {hits.map((hit) => (
                  <li key={hit.call.id}>
                    <SearchHitCard hit={hit} playing={playingId === hit.call.id} onPlay={playHit} />
                  </li>
                ))}
                {list.capped ? (
                  <li>
                    <Tip label={HELP["search.cap"]}>
                      <p tabIndex={0} role="status" className="px-1 py-2 text-xs text-muted">
                        Showing the best {formatNumber(SEARCH_LIST_CAP)} matches. Narrow the date range or add
                        a word to see the rest.
                      </p>
                    </Tip>
                  </li>
                ) : null}
              </ul>
            </>
          )}
        </div>

        {results.data ? (
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
            className="hidden w-64 shrink-0 rounded-md border border-line xl:flex"
          />
        ) : null}
      </div>

      <Dialog
        open={saveOpen}
        onOpenChange={setSaveOpen}
        title="Save this search"
        description="The query and every filter are stored together, so opening it restores this exact view."
        footer={
          <>
            <Button tip="Close without saving" size="sm" onClick={() => setSaveOpen(false)}>
              Cancel
            </Button>
            <Button
              tip="Save it"
              size="sm"
              variant="primary"
              onClick={() =>
                saveSearch.mutate(
                  { name: saveName || filters.q, query: savedQuery(searchParams, filters), shared: false },
                  {
                    onSuccess: () => {
                      toast("Search saved", { tone: "success" });
                      setSaveOpen(false);
                    },
                    onError: () => toast("Could not save the search", { tone: "error" }),
                  },
                )
              }
            >
              Save
            </Button>
          </>
        }
      >
        <TextInput
          value={saveName}
          aria-label="Name for this saved search"
          placeholder="Working fires"
          onChange={(event) => setSaveName(event.target.value)}
        />
      </Dialog>
    </div>
  );
}

/** Ignore a click that is really the end of selecting transcript text, or lands on a control. */
function isPlainCardClick(event: MouseEvent<HTMLElement>): boolean {
  if ((event.target as HTMLElement).closest("a, button, input, [tabindex]")) return false;
  const selection = window.getSelection?.();
  return !selection || selection.isCollapsed;
}

/**
 * One search hit. The card plays the call: a click anywhere outside its links plays it,
 * and the round button is the same action for the keyboard and screen readers. The
 * channel name opens the talkgroup and the time opens the call. The edge on the left is
 * the channel's category colour and turns squelch amber while this call plays, as on Calls.
 * The match score is not shown: the order already says it, and "0.50" meant nothing alone.
 */
const SearchHitCard = memo(function SearchHitCard({
  hit,
  playing,
  onPlay,
}: {
  hit: SearchHit;
  playing: boolean;
  onPlay: (call: Call) => void;
}) {
  const { call } = hit;
  const theme = useTheme();
  const channel = callTalkgroupLabel(call);
  const time = formatCallTime(call.started_at);
  const edge = { "--edge": channelEdgeColor(call.category, theme) } as CSSProperties;
  return (
    // biome-ignore lint/a11y/useKeyWithClickEvents: the play button is the keyboard path; this is a larger mouse target for the same action
    // biome-ignore lint/a11y/noStaticElementInteractions: as above
    <div
      className="key-edge flex cursor-pointer gap-3 overflow-hidden rounded-md border border-line bg-surface py-3 pl-3 pr-3 hover:bg-surface-2"
      data-playing={playing}
      style={edge}
      onClick={(event) => {
        if (isPlainCardClick(event)) onPlay(call);
      }}
    >
      <Tip label={HELP["player.play"]}>
        <button
          type="button"
          aria-label={`Play the call on ${channel}, ${time}`}
          aria-pressed={playing}
          onClick={() => onPlay(call)}
          className={cn(
            "ml-1 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full border",
            playing
              ? "border-squelch text-squelch-ink"
              : "border-control-border text-ink-2 hover:border-accent hover:text-accent-ink",
          )}
        >
          <Play size={14} aria-hidden="true" fill={playing ? "currentColor" : "none"} />
        </button>
      </Tip>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-xs text-ink-2">
          {call.talkgroup_id != null ? (
            <Link
              to={`/talkgroups/${call.talkgroup_id}`}
              className="min-w-0 truncate font-condensed text-[15px] font-semibold text-ink hover:underline"
            >
              {channel}
            </Link>
          ) : (
            <span className="min-w-0 truncate font-condensed text-[15px] font-semibold text-ink">
              {channel}
            </span>
          )}
          <span>{call.radio_label ?? "unresolved"}</span>
          <Link to={`/calls/${call.id}`} className="tabular-nums hover:text-ink hover:underline">
            {time}
          </Link>
          <span className="tabular-nums">{formatDuration(call.duration_s)}</span>
          <QualityDot quality={call.transcript_quality} />
        </div>
        <p className="mt-1 text-sm text-ink">{renderHeadline(hit.highlight)}</p>
      </div>
    </div>
  );
});

/**
 * Says when the results include spelling variants, and which. The variants are shown as
 * the index keeps them — stemmed, so `melros → milros` — which is honest about what was
 * searched; the highlighted excerpts below show the words as they were transcribed.
 */
function FuzzyNotice({
  q,
  fuzzy,
  offerWidening,
  onChange,
}: {
  q: string;
  fuzzy: FuzzySearch | null;
  offerWidening: boolean;
  onChange: (fuzzy: boolean) => void;
}) {
  if (!fuzzy) {
    if (!offerWidening) return null;
    return (
      <p className="text-xs text-muted">
        Only a few exact matches.{" "}
        <Button tip={HELP["search.fuzzy"]} size="sm" variant="ghost" onClick={() => onChange(true)}>
          Include spelling variants
        </Button>
      </p>
    );
  }
  const widened = fuzzy.terms.filter((term) => term.variants.length > 0);
  return (
    <div role="status" className="rounded-md border border-line bg-accent-soft px-3 py-2 text-xs text-ink-2">
      <p>
        {fuzzy.automatic ? (
          <>
            No exact matches for <span className="font-medium text-ink">“{q}”</span>. Showing fuzzy matches
            for its spelling variants:
          </>
        ) : fuzzy.widened ? (
          <>Including spelling variants:</>
        ) : (
          // Either nothing is spelled close to these words, or they are common enough (in
          // 10 000 calls or more) that the API searches them exactly: a word that common
          // is not the one a transcript got wrong.
          <>No spelling variants to add for these words; these are the exact matches.</>
        )}
      </p>
      {widened.length ? (
        <Tip label={HELP["search.fuzzy.notice"]}>
          <ul tabIndex={0} aria-label="Spelling variants searched" className="mt-1 space-y-0.5">
            {widened.map((term) => (
              <li key={term.term} className="font-mono text-[11px]">
                <span className="text-ink">{term.term}</span> → {term.variants.join(", ")}
              </li>
            ))}
          </ul>
        </Tip>
      ) : null}
      {fuzzy.automatic ? null : (
        <Button
          tip={HELP["search.fuzzy.exact"]}
          size="sm"
          variant="ghost"
          className="mt-1"
          onClick={() => onChange(false)}
        >
          Exact matches only
        </Button>
      )}
    </div>
  );
}

/**
 * The query a saved search stores: the URL with its date range spelled out, so the search
 * reopens over the same range even if the page's default ever moves again.
 */
function savedQuery(searchParams: URLSearchParams, filters: CallFilters): string {
  const query = new URLSearchParams(searchParams);
  if (!query.has("preset")) query.set("preset", filters.preset);
  return query.toString();
}

/**
 * How far back to search. Chips rather than the Calls rail, which this page does not
 * have; a custom range from a shared link shows as one more chip with its dates.
 */
function RangePicker({
  filters,
  onChange,
}: {
  filters: CallFilters;
  onChange: (preset: DatePreset) => void;
}) {
  const shown: DatePreset[] = SEARCH_PRESETS.includes(filters.preset)
    ? [...SEARCH_PRESETS]
    : [...SEARCH_PRESETS, filters.preset];
  return (
    <fieldset aria-label="Date range" className="mt-2 flex flex-wrap items-center gap-1.5">
      {shown.map((preset) => (
        <Chip
          key={preset}
          label={preset === "custom" ? `${filters.date_from} – ${filters.date_to}` : PRESET_LABELS[preset]}
          tip={HELP["search.range"]}
          active={filters.preset === preset}
          onClick={() => onChange(preset)}
        />
      ))}
    </fieldset>
  );
}

function SavedSearches({
  saved,
  canWrite,
  onDelete,
}: {
  saved: { id: number; name: string; query: string; shared: boolean }[];
  canWrite: boolean;
  onDelete: (id: number) => void;
}) {
  if (!saved.length) return null;
  return (
    <Card>
      <CardHeader title="Saved searches" hint={HELP["search.saved"]} />
      <ul className="divide-y divide-line">
        {saved.map((item) => (
          <li key={item.id} className="flex items-center gap-2 px-4 py-2">
            <Link
              to={`/search?${item.query}`}
              className="min-w-0 flex-1 truncate text-sm text-ink hover:underline"
            >
              {item.name}
            </Link>
            {item.shared ? <span className="text-[11px] text-muted">shared</span> : null}
            <Button
              tip={canWrite ? HELP["search.saved.delete"] : HELP["app.readOnly"]}
              size="iconSm"
              variant="ghost"
              disabled={!canWrite}
              aria-label={`Delete the saved search ${item.name}`}
              onClick={() => onDelete(item.id)}
            >
              <Trash2 size={14} aria-hidden="true" />
            </Button>
          </li>
        ))}
      </ul>
    </Card>
  );
}
