import { ArrowDown, ArrowUp, Building2, Copy, EyeOff, Pencil, Star, Upload } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router";
import {
  useAgencies,
  useAgencyLabel,
  useCurrentUser,
  useFavorites,
  useRemoveFavorite,
  useSetFavorite,
  useSystems,
  useTalkgroupStatsList,
  useTalkgroups,
  useUpdateTalkgroup,
  useVocabulary,
} from "../api/hooks";
import {
  BULK_MAX_IDS,
  RR_SERVICE_TAGS,
  TALKGROUP_CATEGORIES,
  TALKGROUP_PRIORITY_MAX,
  TALKGROUP_PRIORITY_MIN,
  type Talkgroup,
  type TalkgroupPatch,
  type TalkgroupStat,
} from "../api/types";
import { Sparkline } from "../components/charts/Sparkline";
import { AgencySelect } from "../components/directory/AgencySelect";
import { BulkEditTalkgroupsDrawer } from "../components/directory/BulkEditDrawer";
import { CopyExportFlagsDialog } from "../components/directory/CopyExportFlagsDialog";
import { EditTalkgroupDialog } from "../components/directory/EditTalkgroupDialog";
import { ImportDialog } from "../components/directory/ImportDialog";
import {
  SelectAllCheckbox,
  SelectionBar,
  SelectRowCheckbox,
  toggleInSet,
} from "../components/directory/SelectionBar";
import {
  type ActiveFilter,
  FilterChips,
  TriFilter,
  type TriFilterName as TriName,
  useTalkgroupFilters,
} from "../components/directory/TalkgroupFilters";
import { NOT_TRUNKED_TIP, useTrunkedSystemIds } from "../components/directory/trunked";
import { InfoDot } from "../components/help/InfoDot";
import { PageHeader } from "../components/layout/PageHeader";
import { Button } from "../components/ui/Button";
import { CategoryIcon } from "../components/ui/CategoryIcon";
import { inputClass } from "../components/ui/Field";
import {
  InlineColor,
  InlineNumber,
  InlineSelect,
  InlineSwitch,
  InlineText,
} from "../components/ui/InlineCell";
import { ListPager } from "../components/ui/ListPager";
import { Select } from "../components/ui/Select";
import { SkeletonText } from "../components/ui/Skeleton";
import { EmptyState, ErrorState } from "../components/ui/States";
import { Switch } from "../components/ui/Switch";
import { Tip } from "../components/ui/Tooltip";
import { HELP, useHelpText } from "../help/help";
import { cn } from "../lib/cn";
import { seriesColor } from "../lib/colors";
import {
  agencyLabel,
  CATEGORY_LABELS,
  categoryLabel,
  formatNumber,
  formatRelative,
  talkgroupLabel,
} from "../lib/format";
import { useMediaQuery } from "../lib/useMediaQuery";
import { useTheme } from "../state/theme";
import { mutationErrorToast, toast } from "../state/toasts";

const PAGE_SIZE = 200;

/** As many talkgroups as `GET /stats/talkgroups` will rank; same latency as the default 100. */
const STATS_LIMIT = 500;

/**
 * Every column header that sorts, and the key the API wants for it.
 *
 * One entry per header the reader can click, because a table where four of nineteen
 * headers respond to a click and the rest do not is a table where the reader stops
 * trying. `calls_24h` is the only key with no column behind it — the server counts the
 * last day's traffic for the "Last 24h" sparkline — and the whole map is the page's
 * only statement of what may be sorted: `SORTABLE` is derived from it, so a column
 * cannot claim to sort by a key the request will not carry.
 */
const API_SORT = {
  alias: "alias",
  tgid: "tgid",
  priority: "priority",
  agency: "agency_code",
  category: "category",
  serviceTag: "service_tag",
  record: "record",
  transcribe: "transcribe",
  hidden: "is_hidden",
  shared: "shared",
  exportBroadcastify: "export_broadcastify",
  exportBcfyCalls: "export_bcfy_calls",
  exportOpenmhz: "export_openmhz",
  calls: "call_count",
  spark: "calls_24h",
  lastHeard: "last_seen_at",
} as const;

type SortKey = keyof typeof API_SORT;

/**
 * The "no service tag" sentinel. Not the empty string, which a Radix select keeps
 * for "nothing is selected" — an item using it cannot be chosen back.
 */
const NO_SERVICE_TAG = "none";

type ColumnKey =
  | SortKey
  | "select"
  | "spark"
  | "actions"
  | "favorite"
  | "color"
  | "priority"
  | "agency"
  | "category"
  | "serviceTag"
  | "record"
  | "transcribe"
  | "hidden"
  | "shared"
  | "exportBroadcastify"
  | "exportBcfyCalls"
  | "exportOpenmhz";

/**
 * The columns, in order.
 *
 * `write` marks a column that exists only to change something — the tick box, the star,
 * the swatch, the policy switches, the export flags, the pencil. A reader without write
 * access cannot use any of them, so rather than show a row of greyed-out controls the
 * page drops those columns entirely: on a phone that is eleven columns of dead width
 * between the alias and the last-heard time the guest actually came for.
 */
const COLUMNS: { key: ColumnKey; label: string; help: keyof typeof HELP; write?: true }[] = [
  { key: "select", label: "", help: "app.bulk.selectPage", write: true },
  { key: "favorite", label: "", help: "talkgroups.favorite", write: true },
  { key: "alias", label: "Alias", help: "talkgroups.table.alias" },
  { key: "tgid", label: "TGID", help: "talkgroups.table.tgid" },
  { key: "color", label: "Color", help: "talkgroups.table.color", write: true },
  { key: "priority", label: "Priority", help: "talkgroups.table.priority", write: true },
  { key: "agency", label: "Agency", help: "talkgroups.table.agency" },
  { key: "category", label: "Category", help: "talkgroups.table.category" },
  { key: "serviceTag", label: "Service tag", help: "talkgroups.table.serviceTag" },
  // Record and Transcribe are one decision in two halves — keep the audio, keep the words —
  // so they sit next to each other and are read together.
  { key: "record", label: "Record", help: "talkgroups.table.record", write: true },
  { key: "transcribe", label: "Transcribe", help: "talkgroups.table.transcribe", write: true },
  { key: "hidden", label: "Hidden", help: "talkgroups.table.hidden", write: true },
  { key: "shared", label: "Shared", help: "talkgroups.table.shared", write: true },
  // Broadcastify's two products, each with its own checkbox: the live stream and the
  // per-call archive are separate decisions and only the archive is trunked-only.
  {
    key: "exportBroadcastify",
    label: "BCFY Feed",
    help: "talkgroups.table.exportBroadcastify",
    write: true,
  },
  { key: "exportBcfyCalls", label: "BCFY Calls", help: "talkgroups.table.exportBcfyCalls", write: true },
  { key: "exportOpenmhz", label: "OpenMHz", help: "talkgroups.table.exportOpenmhz", write: true },
  { key: "calls", label: "Calls", help: "talkgroups.table.calls" },
  { key: "spark", label: "Last 24h", help: "talkgroups.table.spark" },
  { key: "lastHeard", label: "Last heard", help: "talkgroups.table.lastHeard" },
  { key: "actions", label: "", help: "talkgroups.edit", write: true },
];

/**
 * What each three-valued filter is called in a chip — the column's own header, so
 * "OpenMHz: off" reads as the column the reader clicked rather than as the wire name
 * `export_openmhz`.
 */
const TRI_LABELS = [
  ["shared", "Shared"],
  ["record", "Record"],
  ["transcribe", "Transcribe"],
  ["export_broadcastify", "BCFY Feed"],
  ["export_bcfy_calls", "BCFY Calls"],
  ["export_openmhz", "OpenMHz"],
] as const;

/** The column each three-valued filter lives under, by the API's name for it. */
const TRI_KEYS = new Map<ColumnKey, TriName>([
  ["record", "record"],
  ["transcribe", "transcribe"],
  ["shared", "shared"],
  ["exportBroadcastify", "export_broadcastify"],
  ["exportBcfyCalls", "export_bcfy_calls"],
  ["exportOpenmhz", "export_openmhz"],
]);

/** Which column headers sort: exactly the ones the API has a key for. */
const SORTABLE = new Set<string>(Object.keys(API_SORT));

/** The API sort key a column header asks for, or null when the header is a label. */
const sortKeyFor = (column: ColumnKey): string | null =>
  SORTABLE.has(column) ? API_SORT[column as SortKey] : null;

/**
 * The channel directory, where the edits happen.
 *
 * The short policy fields — alias, color, priority, agency, category, service tag,
 * and the three switches — are edited in the grid itself, because each is a single
 * value the column header already explains and the operator changes several of
 * them in a sitting. Sending one of them through a pencil, a modal, a Save and a
 * close was four interactions for a rename. The fields that need a sentence of
 * context or interact with each other — notes, retention, the minimum length to
 * transcribe — stay behind the pencil, where there is room to explain them.
 *
 * Search, sort and paging are all the server's: the endpoint caps a page at 500
 * rows, so a page that filtered and sorted what it happened to have been given
 * would be answering about the first 500 channels and calling it the directory.
 */
export function Talkgroups() {
  const [editing, setEditing] = useState<Talkgroup | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [copyOpen, setCopyOpen] = useState(false);
  // The tick set lives on the page, not in the table: the operator gathers a
  // selection by ticking, paging, and ticking more, so it has to outlive the rows.
  const [picked, setPicked] = useState<ReadonlySet<number>>(() => new Set());
  const [bulkOpen, setBulkOpen] = useState(false);
  const { canWrite, isAdmin } = useCurrentUser();
  const mode = useTheme();
  // The table needs `md`; below it each channel is a card.
  const tableFits = useMediaQuery("(min-width: 768px)");
  // Read into a title, outside a tooltip: resolved so it is never the loading placeholder.
  const manageAgenciesHelp = useHelpText("talkgroups.manageAgencies");

  // Every filter, the sort and the page live in the query string rather than in this
  // component: a filtered directory is something operators send each other, and it has
  // to survive a reload, a back button and a link in a message.
  const { state, set, clearAll, signature, params } = useTalkgroupFilters(PAGE_SIZE);
  const { sort, page } = state;
  const descending = sort.startsWith("-");
  const sortKey = descending ? sort.slice(1) : sort;
  const showHidden = state.hidden;

  // The search box is the one control that is not written straight to the URL: it goes
  // in debounced, so a typed word is one request and one history entry rather than one
  // per keystroke. `pushed` is what this page last put there, which is how a chip
  // removing the search — a change from outside the box — gets back into the box.
  const [query, setQuery] = useState(state.q);
  const pushed = useRef(state.q);
  useEffect(() => {
    if (state.q !== pushed.current) {
      pushed.current = state.q;
      setQuery(state.q);
    }
  }, [state.q]);
  useEffect(() => {
    const handle = setTimeout(() => {
      const trimmed = query.trim();
      if (trimmed === pushed.current) return;
      pushed.current = trimmed;
      set("q", trimmed || null);
    }, 250);
    return () => clearTimeout(handle);
  }, [query, set]);

  const talkgroups = useTalkgroups(params);
  // The pickers the filter row offers. The vocabulary is what a channel *may* be
  // filed under, not what this deployment happens to have used, so a category with
  // nothing in it is still offered — and answers honestly, with an empty table.
  const vocabulary = useVocabulary();
  const agencies = useAgencies();
  const agencyName = useAgencyLabel();
  const systems = useSystems();
  // The server's ranking cap: the default hundred left most of a 200-row page without a trend.
  const stats = useTalkgroupStatsList({}, STATS_LIMIT);
  // A read-only viewer has no star to fill in and no Calls checkbox to fix off, so
  // neither list is fetched for them: a request nothing on the page can use.
  const favorites = useFavorites(canWrite);
  const addFavorite = useSetFavorite();
  const removeFavorite = useRemoveFavorite();
  const update = useUpdateTalkgroup();

  const favoriteIds = useMemo(
    () => new Set((favorites.data ?? []).map((favorite) => favorite.talkgroup_id)),
    [favorites.data],
  );
  const statsById = useMemo(
    () => new Map((stats.data ?? []).map((stat: TalkgroupStat) => [stat.talkgroup_id, stat])),
    [stats.data],
  );

  // A favorited channel outranks the sort — that is what favoriting is for — but
  // only within the page on screen: floating one in from page 4 would leave a hole
  // in a count the pager has already told the reader about.
  const rows = useMemo(() => {
    const items = talkgroups.data?.items ?? [];
    return [...items].sort((a, b) => Number(favoriteIds.has(b.id)) - Number(favoriteIds.has(a.id)));
  }, [talkgroups.data, favoriteIds]);

  /**
   * Send one field. `exclude_unset` on the server means a patch body carrying a
   * single key writes a single column, so two people editing different columns of
   * the same row do not overwrite each other — which a whole-row PUT would.
   */
  const patchField = (talkgroup: Talkgroup, patch: TalkgroupPatch) =>
    update.mutateAsync({ id: talkgroup.id, patch });

  const total = talkgroups.data?.total ?? 0;
  const pageIds = useMemo(() => rows.map((row) => row.id), [rows]);
  const pickedIds = useMemo(() => [...picked], [picked]);
  // Which systems Broadcastify Calls may publish, so the Calls column can be shown fixed
  // off — with the reason — on the ones it never could.
  const trunkedSystems = useTrunkedSystemIds(canWrite);
  // The columns this viewer can actually act on. See `COLUMNS`.
  const columns = useMemo(() => COLUMNS.filter((column) => canWrite || !column.write), [canWrite]);
  // What is being asked, as removable chips. Built from the state rather than from the
  // controls, so a filter that arrived in a pasted link is offered back even when its
  // column is off the right of the screen — or, for a guest, not on screen at all.
  const activeFilters: ActiveFilter[] = useMemo(() => {
    const drop = (key: keyof typeof state) => () => set(key, null);
    const found: ActiveFilter[] = [];
    if (state.q) found.push({ key: "q", label: `Search: ${state.q}`, onRemove: drop("q") });
    if (state.system_id) {
      const system = (systems.data ?? []).find((row) => String(row.id) === state.system_id);
      found.push({
        key: "system_id",
        label: `System: ${system?.name ?? state.system_id}`,
        onRemove: drop("system_id"),
      });
    }
    if (state.category) {
      found.push({
        key: "category",
        label: `Category: ${categoryLabel(state.category)}`,
        onRemove: drop("category"),
      });
    }
    if (state.service_tag) {
      found.push({
        key: "service_tag",
        label: `Service tag: ${state.service_tag}`,
        onRemove: drop("service_tag"),
      });
    }
    if (state.agency) {
      const agency = (agencies.data ?? []).find((row) => row.code === state.agency);
      found.push({
        key: "agency",
        label: `Agency: ${agencyLabel(state.agency, agency)}`,
        onRemove: drop("agency"),
      });
    }
    for (const [name, label] of TRI_LABELS) {
      const value = state[name];
      if (value) {
        found.push({
          key: name,
          label: `${label}: ${value === "true" ? "on" : "off"}`,
          onRemove: drop(name),
        });
      }
    }
    if (state.hidden) {
      found.push({
        key: "hidden",
        label: "Hidden channels shown",
        onRemove: () => set("hidden", false),
      });
    }
    return found;
  }, [state, set, agencies.data, systems.data]);

  const checkedHere = pageIds.filter((id) => picked.has(id)).length;
  const allOnPageChecked = pageIds.length > 0 && checkedHere === pageIds.length;
  const someOnPageChecked = checkedHere > 0;

  // Changing any filter, the sort or the page is changing the question. A tick that
  // survives it belongs to rows the reader can no longer see, and "Edit 41" would then
  // be editing something they are not looking at. One dependency now that the whole
  // question is in the query string, which is also the only way to be sure a filter
  // added later cannot be forgotten here.
  // biome-ignore lint/correctness/useExhaustiveDependencies: clear on the question only
  useEffect(() => {
    setPicked(new Set());
  }, [signature]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Talkgroups"
        description={
          canWrite
            ? "Every channel the recorder has heard, how busy each one is, and the per-channel transcription and retention policies. Click a value to change it."
            : "Every channel the recorder has heard, what it is, and how busy it has been."
        }
        actions={
          <>
            {/* Said once, at the top, rather than on every one of eight editable
                columns: Enter saves and Escape reverts are not discoverable from
                a cell that merely looks clickable. */}
            {canWrite ? <InfoDot id="app.inlineEdit" /> : null}
            <Switch
              checked={showHidden}
              onCheckedChange={(next) => set("hidden", next)}
              label="Show hidden"
              tip={HELP["talkgroups.showHidden"]}
            />
            {/* The system is the one filter with no column of its own — every row on a
                single-system deployment would carry the same word — so it sits in the
                toolbar rather than in the filter row, and only when there is a choice
                to make. */}
            {(systems.data ?? []).length > 1 ? (
              <Select
                value={state.system_id}
                onValueChange={(next) => set("system_id", next || null)}
                options={[
                  { value: "", label: "Every system" },
                  ...(systems.data ?? []).map((system) => ({
                    value: String(system.id),
                    label: system.name,
                  })),
                ]}
                tip={HELP["talkgroups.filter.system"]}
                ariaLabel="Filter by system"
                size="sm"
                className="h-8 w-40 text-xs"
              />
            ) : null}
            <Tip label={HELP["talkgroups.search"]}>
              <input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Alias or TGID…"
                aria-label="Search talkgroups"
                // `cn`, not a template literal: `inputClass` carries `w-full` and
                // `h-9`, and in a plain concatenation those win on cascade order —
                // the search box rendered 513px wide, which pushed the toolbar to
                // three rows and cost the table a hundred pixels of height.
                className={cn(inputClass, "h-8 w-48")}
              />
            </Tip>
            {/* Not merely disabled for a read-only viewer: a greyed-out "Import
                playlist" is an offer the page will never honour for them, and it
                crowds a phone-width toolbar that still has to carry the search. */}
            {/* The agency column is a picker over a table an operator maintains
                elsewhere, and "elsewhere" was previously something you had to know.
                Admin only, because it is the only role the tab lets in. */}
            {isAdmin ? (
              <Link
                to="/admin?tab=agencies"
                title={manageAgenciesHelp}
                className="inline-flex items-center gap-1.5 text-xs font-medium text-accent hover:underline"
              >
                <Building2 size={14} aria-hidden="true" />
                Manage agencies
              </Link>
            ) : null}
            {canWrite ? (
              <>
                <Button tip={HELP["talkgroups.copyFlags"]} size="sm" onClick={() => setCopyOpen(true)}>
                  <Copy size={14} aria-hidden="true" /> Copy export flags
                </Button>
                <Button tip={HELP["talkgroups.import"]} size="sm" onClick={() => setImportOpen(true)}>
                  <Upload size={14} aria-hidden="true" /> Import playlist
                </Button>
              </>
            ) : null}
          </>
        }
      />

      {/* `relative` is load-bearing, for the reason AppLayout gives one level up: the
          rows are full of `sr-only` captions and labels, every one of them absolutely
          positioned. Without a positioned ancestor here they resolve against `<main>`
          instead, and this box's `overflow` does not clip them — so a page of twenty
          rows gave *main* 400px of scrollable overflow it had no content for. The
          window grew a second scrollbar, and scrolling it slid the whole fixed-height
          page up and left blank space under the table. This makes this box their
          containing block, so they are clipped with the rows they belong to and the
          table region is the only thing that scrolls. */}
      <div className="relative min-h-0 flex-1 overflow-auto scrollbar-thin px-4 pb-6 md:px-6">
        <FilterChips filters={activeFilters} onClearAll={clearAll} />
        {talkgroups.error ? (
          <ErrorState error={talkgroups.error} onRetry={() => void talkgroups.refetch()} />
        ) : talkgroups.isLoading && !talkgroups.data ? (
          <SkeletonText lines={10} />
        ) : rows.length === 0 ? (
          <EmptyState
            title="No talkgroups match"
            hint={
              // The filter row is inside the table, and the table is what has just
              // gone away — so the way out has to be named here, and the chips above
              // are the control that does it.
              activeFilters.length > 0
                ? "Remove a filter above, or clear them all, to widen the question."
                : showHidden
                  ? "Clear the search box to see the whole directory."
                  : "Clear the search box, or turn on 'Show hidden' — a hidden channel is still in the archive."
            }
          />
        ) : !tableFits ? (
          // Below `md`, cards: the key fields of each channel, the way `CallCards` does the
          // calls. The inline editors, the policy switches and the filter row are desk
          // work; on a phone the pencil opens the same editor, and the chips above still
          // show (and clear) every filter.
          <ul aria-label="Talkgroups" className="divide-y divide-line">
            {rows.map((talkgroup) => {
              const name = talkgroupLabel(talkgroup);
              const favorite = favoriteIds.has(talkgroup.id);
              return (
                <li
                  key={talkgroup.id}
                  className={cn("flex items-start gap-2 py-2.5", talkgroup.is_hidden && "opacity-60")}
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <CategoryIcon
                        category={talkgroup.category}
                        serviceTag={talkgroup.service_tag}
                        interactive={false}
                      />
                      {talkgroup.is_hidden ? (
                        <span className="shrink-0 text-muted">
                          <EyeOff size={13} aria-hidden="true" />
                          <span className="sr-only">Hidden from the default views</span>
                        </span>
                      ) : null}
                      <Link
                        to={`/talkgroups/${talkgroup.id}`}
                        className="block min-w-0 truncate text-sm font-medium text-ink hover:underline"
                      >
                        {name}
                      </Link>
                      <span className="shrink-0 text-[11px] tabular-nums text-muted">{talkgroup.tgid}</span>
                      <span className="ml-auto shrink-0 text-[11px] text-ink-2">
                        {formatRelative(talkgroup.last_seen_at)}
                      </span>
                    </div>
                    <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[11px] text-ink-2">
                      <span>{categoryLabel(talkgroup.category ?? "other")}</span>
                      {talkgroup.service_tag ? <span>{talkgroup.service_tag}</span> : null}
                      {talkgroup.agency_code ? <span>{agencyName(talkgroup.agency_code)}</span> : null}
                      <span className="tabular-nums">{formatNumber(talkgroup.call_count)} calls</span>
                    </div>
                  </div>
                  {canWrite ? (
                    <span className="flex shrink-0 items-center">
                      <Button
                        tip={HELP["talkgroups.favorite"]}
                        size="iconSm"
                        variant="ghost"
                        aria-pressed={favorite}
                        aria-label={favorite ? `Remove ${name} from favorites` : `Add ${name} to favorites`}
                        onClick={() => {
                          const onError = () => toast("Could not save the favorite", { tone: "error" });
                          if (favorite) removeFavorite.mutate(talkgroup.id, { onError });
                          else addFavorite.mutate({ talkgroup_id: talkgroup.id, muted: false }, { onError });
                        }}
                        className={favorite ? "text-accent-ink" : undefined}
                      >
                        <Star size={14} aria-hidden="true" fill={favorite ? "currentColor" : "none"} />
                      </Button>
                      <Button
                        tip={HELP["talkgroups.edit"]}
                        size="iconSm"
                        variant="ghost"
                        aria-label={`Edit ${name}`}
                        onClick={() => setEditing(talkgroup)}
                      >
                        <Pencil size={14} aria-hidden="true" />
                      </Button>
                    </span>
                  ) : null}
                </li>
              );
            })}
          </ul>
        ) : (
          <table className="w-full min-w-[48rem] text-left text-sm">
            <caption className="sr-only">
              {canWrite
                ? "Talkgroups with their policies, call counts, last-24-hour activity and when each was last heard. The alias, color, priority, agency, category, service tag, record, transcribe, hidden, shared and the three export columns are edited in place."
                : "Talkgroups with their agency, category and service tag, call counts, last-24-hour activity and when each was last heard."}
            </caption>
            <thead className="sticky top-0 z-10 bg-page">
              <tr>
                {columns.map((column) => {
                  const key = sortKeyFor(column.key);
                  const sorted = key !== null && key === sortKey;
                  return (
                    <th
                      key={column.key}
                      scope="col"
                      aria-sort={sorted ? (descending ? "descending" : "ascending") : undefined}
                      // Sentence case: a sorting header is a <button>, which does not inherit
                      // `uppercase`, so the row used to mix "Alias" with "LAST 24H".
                      className="border-b border-line px-2 py-2 text-[11px] font-semibold text-ink-2"
                    >
                      {column.key === "select" ? (
                        <SelectAllCheckbox
                          allChecked={allOnPageChecked}
                          someChecked={someOnPageChecked}
                          onChange={(on) => setPicked((current) => toggleInSet(current, pageIds, on))}
                        />
                      ) : key === null ? (
                        column.label ? (
                          <Tip label={HELP[column.help]}>
                            <span tabIndex={0}>{column.label}</span>
                          </Tip>
                        ) : (
                          <span className="sr-only">
                            {column.key === "favorite" ? "Favorite" : "Actions"}
                          </span>
                        )
                      ) : (
                        <Tip label={`${HELP[column.help]}. ${HELP["calls.table.sort"]}`}>
                          <button
                            type="button"
                            aria-label={`Sort by ${column.label}`}
                            // A second click on the column already sorted turns it
                            // around; a first click on any other starts descending,
                            // which is the interesting end of every one of these
                            // columns — the busiest, the most recent, the ones that
                            // are on.
                            onClick={() => set("sort", sorted && descending ? key : `-${key}`)}
                            className={cn(
                              "inline-flex items-center gap-1 hover:text-ink",
                              sorted && "text-accent-ink",
                            )}
                          >
                            {column.label}
                            {sorted ? (
                              descending ? (
                                <ArrowDown size={11} aria-hidden="true" />
                              ) : (
                                <ArrowUp size={11} aria-hidden="true" />
                              )
                            ) : null}
                          </button>
                        </Tip>
                      )}
                    </th>
                  );
                })}
              </tr>
              {/* The filter row: one control under the header it narrows, so "which of
                  these am I looking at" is asked in the same place the column is read.
                  `td` rather than `th` because these are controls, not headings — and
                  a screen reader announcing nineteen extra column headers would bury
                  the ones that name the data. */}
              <tr className="bg-page">
                {columns.map((column) => (
                  <td
                    key={column.key}
                    className="border-b border-line px-2 pb-2 align-top font-normal normal-case"
                  >
                    {column.key === "category" ? (
                      <Select
                        value={state.category}
                        onValueChange={(next) => set("category", next || null)}
                        options={[
                          { value: "", label: "any" },
                          ...(vocabulary.data?.categories ?? []).map((term) => ({
                            value: term.key,
                            label: term.label,
                          })),
                        ]}
                        tip={HELP["talkgroups.table.category"]}
                        ariaLabel="Filter by category"
                        size="sm"
                        className="h-7 w-full text-xs"
                      />
                    ) : column.key === "serviceTag" ? (
                      <Select
                        value={state.service_tag}
                        onValueChange={(next) => set("service_tag", next || null)}
                        options={[
                          { value: "", label: "any" },
                          ...(vocabulary.data?.service_tags ?? []).map((term) => ({
                            value: term.tag,
                            label: term.tag,
                          })),
                        ]}
                        tip={HELP["talkgroups.table.serviceTag"]}
                        ariaLabel="Filter by service tag"
                        size="sm"
                        className="h-7 w-full text-xs"
                      />
                    ) : column.key === "agency" ? (
                      <Select
                        value={state.agency}
                        onValueChange={(next) => set("agency", next || null)}
                        options={[
                          { value: "", label: "any" },
                          // Inactive agencies are still offered here: they are why an
                          // operator opens this filter at all — to find what is still
                          // mapped to one that has been retired.
                          ...(agencies.data ?? []).map((agency) => ({
                            value: agency.code,
                            label: `${agency.name} (${agency.code})`,
                          })),
                        ]}
                        tip={HELP["talkgroups.table.agency"]}
                        ariaLabel="Filter by agency"
                        size="sm"
                        className="h-7 w-full text-xs"
                      />
                    ) : TRI_KEYS.has(column.key) ? (
                      <TriFilter
                        value={state[TRI_KEYS.get(column.key) as TriName]}
                        onChange={(next) => set(TRI_KEYS.get(column.key) as TriName, next || null)}
                        label={column.label}
                        tip={HELP[column.help]}
                      />
                    ) : null}
                  </td>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((talkgroup) => {
                const stat = statsById.get(talkgroup.id);
                const name = talkgroupLabel(talkgroup);
                return (
                  <tr
                    key={talkgroup.id}
                    className={cn(
                      "border-b border-line hover:bg-surface-2",
                      // A hidden channel is dimmed rather than dropped, so it is
                      // obvious *why* it looks different from its neighbours.
                      talkgroup.is_hidden && "opacity-60",
                    )}
                  >
                    {canWrite ? (
                      <td className="w-6 px-1 py-1.5">
                        <SelectRowCheckbox
                          checked={picked.has(talkgroup.id)}
                          label={name}
                          onChange={(on) => setPicked((current) => toggleInSet(current, [talkgroup.id], on))}
                        />
                      </td>
                    ) : null}

                    {canWrite ? (
                      <td className="w-8 px-1 py-1.5">
                        <Button
                          tip={HELP["talkgroups.favorite"]}
                          size="iconSm"
                          variant="ghost"
                          aria-pressed={favoriteIds.has(talkgroup.id)}
                          aria-label={
                            favoriteIds.has(talkgroup.id)
                              ? `Remove ${name} from favorites`
                              : `Add ${name} to favorites`
                          }
                          onClick={() => {
                            const onError = () => toast("Could not save the favorite", { tone: "error" });
                            if (favoriteIds.has(talkgroup.id)) {
                              removeFavorite.mutate(talkgroup.id, { onError });
                            } else {
                              addFavorite.mutate({ talkgroup_id: talkgroup.id, muted: false }, { onError });
                            }
                          }}
                          className={favoriteIds.has(talkgroup.id) ? "text-accent-ink" : undefined}
                        >
                          <Star
                            size={14}
                            aria-hidden="true"
                            fill={favoriteIds.has(talkgroup.id) ? "currentColor" : "none"}
                          />
                        </Button>
                      </td>
                    ) : null}

                    <td className="min-w-56 px-2 py-1.5">
                      <span className="flex items-center gap-1.5">
                        <CategoryIcon category={talkgroup.category} serviceTag={talkgroup.service_tag} />
                        {talkgroup.is_hidden ? (
                          <Tip label={HELP["talkgroups.table.hidden"]}>
                            <span tabIndex={0} className="text-muted">
                              <EyeOff size={13} aria-label="Hidden from the default views" />
                            </span>
                          </Tip>
                        ) : null}
                        <InlineText
                          value={talkgroup.alias}
                          onSave={(next) => patchField(talkgroup, { alias: next })}
                          label={`Alias for talkgroup ${talkgroup.tgid}`}
                          tip={HELP["talkgroups.edit.alias"]}
                          placeholder={`TG ${talkgroup.tgid}`}
                          hint="Name this channel — the alias every list shows it under."
                          disabled={!canWrite}
                          emptyAsNull
                          // Grows with the column rather than stopping at a fixed
                          // 18rem: on a wide monitor the alias column is twice that,
                          // and a cap measured in rem left the name truncated beside
                          // empty space. `min-w-0` keeps the truncation working when
                          // the window is narrow enough to need it.
                          className="min-w-0 flex-1 font-medium text-ink"
                        />
                        <Tip label={HELP["talkgroups.detail.stats"]}>
                          <Link
                            to={`/talkgroups/${talkgroup.id}`}
                            aria-label={`Open ${name}`}
                            className="shrink-0 text-[11px] text-accent-ink hover:underline"
                          >
                            open
                          </Link>
                        </Tip>
                      </span>
                      {talkgroup.group_name ? (
                        <span className="block max-w-full truncate pl-1 text-[11px] text-muted">
                          {talkgroup.group_name}
                        </span>
                      ) : null}
                    </td>

                    <td className="px-2 py-1.5 tabular-nums text-ink-2">{talkgroup.tgid}</td>

                    {canWrite ? (
                      <td className="px-2 py-1.5">
                        <InlineColor
                          value={talkgroup.color}
                          onSave={(next) => patchField(talkgroup, { color: next })}
                          label={`Color for ${name}`}
                          tip={HELP["talkgroups.edit.color"]}
                        />
                      </td>
                    ) : null}

                    {canWrite ? (
                      <td className="px-2 py-1.5">
                        <InlineNumber
                          value={talkgroup.priority}
                          onSave={(next) => patchField(talkgroup, { priority: next })}
                          label={`Priority for ${name}`}
                          tip={HELP["talkgroups.edit.priority"]}
                          min={TALKGROUP_PRIORITY_MIN}
                          max={TALKGROUP_PRIORITY_MAX}
                        />
                      </td>
                    ) : null}

                    <td className="w-40 px-2 py-1.5">
                      {canWrite ? (
                        <AgencySelect
                          value={talkgroup.agency_code}
                          onValueChange={(code) => {
                            void patchField(talkgroup, { agency_code: code }).catch((error: unknown) =>
                              mutationErrorToast(error, "Could not save the agency"),
                            );
                          }}
                          tip={HELP["talkgroups.edit.agency"]}
                          ariaLabel={`Agency for ${name}`}
                          size="sm"
                          className="h-7 w-full text-xs"
                        />
                      ) : (
                        <span className="text-xs text-ink-2">{agencyName(talkgroup.agency_code)}</span>
                      )}
                    </td>

                    {/* Category and service tag are values a guest is entitled to read,
                        so they stay — as text. A disabled select would show the same
                        word inside a control that cannot be opened. */}
                    <td className="w-32 px-2 py-1.5">
                      {canWrite ? (
                        <InlineSelect
                          value={talkgroup.category ?? "other"}
                          onSave={(next) => patchField(talkgroup, { category: next })}
                          label={`Category for ${name}`}
                          tip={HELP["talkgroups.edit.category"]}
                          options={TALKGROUP_CATEGORIES.map((category) => ({
                            value: category,
                            label: CATEGORY_LABELS[category] ?? category,
                          }))}
                        />
                      ) : (
                        <span className="text-xs text-ink-2">
                          {categoryLabel(talkgroup.category ?? "other")}
                        </span>
                      )}
                    </td>

                    <td className="w-40 px-2 py-1.5">
                      {canWrite ? (
                        <InlineSelect
                          value={talkgroup.service_tag ?? NO_SERVICE_TAG}
                          onSave={(next) =>
                            patchField(talkgroup, {
                              service_tag: next === NO_SERVICE_TAG ? null : next,
                            })
                          }
                          label={`Service tag for ${name}`}
                          tip={HELP["talkgroups.edit.serviceTag"]}
                          options={[
                            { value: NO_SERVICE_TAG, label: "— none —" },
                            ...RR_SERVICE_TAGS.map((tag) => ({ value: tag, label: tag })),
                          ]}
                        />
                      ) : (
                        <span className="text-xs text-ink-2">{talkgroup.service_tag ?? "—"}</span>
                      )}
                    </td>

                    {canWrite ? (
                      <>
                        <td className="px-2 py-1.5">
                          <InlineSwitch
                            value={talkgroup.record}
                            onSave={(next) => patchField(talkgroup, { record: next })}
                            label={`Keep the audio of ${name}`}
                            tip={HELP["talkgroups.edit.record"]}
                          />
                        </td>

                        <td className="px-2 py-1.5">
                          <InlineSwitch
                            value={talkgroup.transcribe}
                            onSave={(next) => patchField(talkgroup, { transcribe: next })}
                            label={`Transcribe ${name}`}
                            tip={HELP["talkgroups.edit.transcribe"]}
                          />
                        </td>

                        <td className="px-2 py-1.5">
                          <InlineSwitch
                            value={talkgroup.is_hidden}
                            onSave={(next) => patchField(talkgroup, { is_hidden: next })}
                            label={`Hide ${name} from the default views`}
                            tip={HELP["talkgroups.edit.hidden"]}
                          />
                        </td>

                        <td className="px-2 py-1.5">
                          <InlineSwitch
                            value={talkgroup.shared}
                            onSave={(next) => patchField(talkgroup, { shared: next })}
                            label={`${name} is a shared county channel`}
                            tip={HELP["talkgroups.edit.shared"]}
                          />
                        </td>
                      </>
                    ) : null}

                    {/* The three export checkboxes.  They do nothing on their own:
                        the exporters read them only when their talkgroup selection is
                        `allow` or `deny` (Admin → Exports), and every service ships off.
                        Broadcastify's two products have one each — the live Feed and the
                        per-call Calls archive — and Calls is offered only where it could
                        publish at all. */}
                    {canWrite ? (
                      <>
                        <td className="px-2 py-1.5">
                          <InlineSwitch
                            value={talkgroup.export_broadcastify}
                            onSave={(next) => patchField(talkgroup, { export_broadcastify: next })}
                            label={`Carry ${name} on the Broadcastify Feed`}
                            tip={HELP["talkgroups.table.exportBroadcastify"]}
                          />
                        </td>

                        <td className="px-2 py-1.5">
                          {trunkedSystems.has(talkgroup.system_id) ? (
                            <InlineSwitch
                              value={talkgroup.export_bcfy_calls}
                              onSave={(next) => patchField(talkgroup, { export_bcfy_calls: next })}
                              label={`Publish ${name} to Broadcastify Calls`}
                              tip={HELP["talkgroups.table.exportBcfyCalls"]}
                            />
                          ) : (
                            // Fixed off rather than disabled-but-ticked: Calls would refuse
                            // the call, and a box that looks on is a promise this page cannot
                            // keep.
                            <Tip label={NOT_TRUNKED_TIP}>
                              <span
                                className="cursor-help text-[11px] text-muted"
                                title={`Broadcastify Calls is unavailable for ${name}: its system is not trunked`}
                              >
                                not trunked
                              </span>
                            </Tip>
                          )}
                        </td>

                        <td className="px-2 py-1.5">
                          <InlineSwitch
                            value={talkgroup.export_openmhz}
                            onSave={(next) => patchField(talkgroup, { export_openmhz: next })}
                            label={`Publish ${name} to OpenMHz`}
                            tip={HELP["talkgroups.table.exportOpenmhz"]}
                          />
                        </td>
                      </>
                    ) : null}

                    <td className="px-2 py-1.5 tabular-nums text-ink-2">
                      {formatNumber(talkgroup.call_count)}
                    </td>
                    <td className="px-2 py-1.5">
                      <Sparkline
                        values={stat?.spark ?? []}
                        loading={stats.isLoading}
                        color={seriesColor(name, mode)}
                        label={`${name}: calls per hour over the last 24 hours`}
                      />
                    </td>
                    <td className="px-2 py-1.5 text-xs text-ink-2">
                      {formatRelative(talkgroup.last_seen_at)}
                    </td>
                    {canWrite ? (
                      <td className="px-2 py-1.5 text-right">
                        <Button
                          tip={HELP["talkgroups.edit"]}
                          size="iconSm"
                          variant="ghost"
                          aria-label={`Edit ${name}`}
                          onClick={() => setEditing(talkgroup)}
                        >
                          <Pencil size={14} aria-hidden="true" />
                        </Button>
                      </td>
                    ) : null}
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}

        {canWrite ? (
          <SelectionBar
            count={picked.size}
            noun="talkgroups"
            loadedCount={rows.length}
            canWrite={canWrite}
            atLimit={picked.size > BULK_MAX_IDS}
            onEdit={() => setBulkOpen(true)}
            onClear={() => setPicked(new Set())}
            onSelectAllLoaded={() => setPicked((current) => toggleInSet(current, pageIds, true))}
          />
        ) : null}

        {total > 0 && rows.length > 0 ? (
          <ListPager
            page={page}
            pageSize={PAGE_SIZE}
            total={total}
            onPageChange={(next) => set("page", next)}
            noun="talkgroups"
          />
        ) : null}
      </div>

      {/* The editing surfaces are not merely unopenable for a read-only viewer, they are
          not mounted: each one loads the pickers it would need to offer. */}
      {canWrite ? (
        <>
          <BulkEditTalkgroupsDrawer open={bulkOpen} onOpenChange={setBulkOpen} ids={pickedIds} />
          <EditTalkgroupDialog
            talkgroup={editing}
            open={editing !== null}
            onOpenChange={(open) => !open && setEditing(null)}
          />
          <CopyExportFlagsDialog open={copyOpen} onOpenChange={setCopyOpen} ids={pickedIds} />
          <ImportDialog kind="talkgroups" open={importOpen} onOpenChange={setImportOpen} />
        </>
      ) : null}
    </div>
  );
}
