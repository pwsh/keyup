import { ArrowDown, ArrowUp, Pencil, Upload } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import {
  useAgencyLabel,
  useCurrentUser,
  useRadioStatsList,
  useRadios,
  useSystems,
  useUpdateRadio,
} from "../api/hooks";
import {
  BULK_MAX_IDS,
  RADIO_KINDS,
  type Radio,
  type RadioKind,
  type RadioPatch,
  type RadioStat,
} from "../api/types";
import { Sparkline } from "../components/charts/Sparkline";
import { AgencySelect } from "../components/directory/AgencySelect";
import { BulkEditRadiosDrawer } from "../components/directory/BulkEditDrawer";
import { EditRadioDialog } from "../components/directory/EditRadioDialog";
import { ImportDialog } from "../components/directory/ImportDialog";
import {
  SelectAllCheckbox,
  SelectionBar,
  SelectRowCheckbox,
  toggleInSet,
} from "../components/directory/SelectionBar";
import { PageHeader } from "../components/layout/PageHeader";
import { Button } from "../components/ui/Button";
import { inputClass } from "../components/ui/Field";
import { InlineSelect, InlineText } from "../components/ui/InlineCell";
import { ListPager } from "../components/ui/ListPager";
import { Select } from "../components/ui/Select";
import { SkeletonText } from "../components/ui/Skeleton";
import { EmptyState, ErrorState } from "../components/ui/States";
import { Tip } from "../components/ui/Tooltip";
import { HELP } from "../help/help";
import { cn } from "../lib/cn";
import { seriesColor } from "../lib/colors";
import { formatNumber, formatRelative, radioLabel } from "../lib/format";
import { useMediaQuery } from "../lib/useMediaQuery";
import { useTheme } from "../state/theme";
import { mutationErrorToast } from "../state/toasts";

type SortKey = "unit" | "radioId" | "calls" | "lastHeard";
type Activity = "all" | "transmitted" | "seen";

const PAGE_SIZE = 200;

/** The page's sort key mapped to the API's, so the server does the ordering. */
const API_SORT: Record<SortKey, string> = {
  unit: "unit_id",
  radioId: "radio_id",
  calls: "call_count",
  lastHeard: "last_seen_at",
};

const ACTIVITY_OPTIONS = [
  { value: "all", label: "All radios" },
  { value: "transmitted", label: "Transmitted" },
  { value: "seen", label: "Seen only" },
];

type ColumnKey = SortKey | "select" | "spark" | "top" | "actions" | "alias" | "kind" | "agency" | "system";

/** As many radios as `GET /stats/radios` will rank, so a 200-row page is not mostly "—". */
const STATS_LIMIT = 500;

/** `write` marks a column that is only a control; a read-only viewer never sees it. */
const COLUMNS: { key: ColumnKey; label: string; help: keyof typeof HELP; write?: true }[] = [
  { key: "select", label: "", help: "app.bulk.selectPage", write: true },
  { key: "unit", label: "Unit", help: "radios.table.unit" },
  { key: "alias", label: "Alias", help: "radios.edit.alias" },
  { key: "radioId", label: "Radio ID", help: "radios.table.radioId" },
  { key: "system", label: "System", help: "radios.table.system" },
  { key: "kind", label: "Kind", help: "radios.edit.kind" },
  { key: "agency", label: "Agency", help: "radios.edit.agency" },
  { key: "calls", label: "Calls", help: "radios.table.calls" },
  { key: "spark", label: "Last 24h", help: "radios.table.spark" },
  { key: "top", label: "Most used channel", help: "radios.table.topTalkgroup" },
  { key: "lastHeard", label: "Last heard", help: "radios.table.lastHeard" },
  { key: "actions", label: "", help: "radios.edit", write: true },
];

/** Which column headers sort. The rest label a value or an inline control. */
const SORTABLE = new Set<ColumnKey>(["unit", "radioId", "calls", "lastHeard"]);

/** The radio and unit directory. */
export function Radios() {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<SortKey>("calls");
  const [descending, setDescending] = useState(true);
  const [activity, setActivity] = useState<Activity>("all");
  const [page, setPage] = useState(1);
  const [editing, setEditing] = useState<Radio | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [picked, setPicked] = useState<ReadonlySet<number>>(() => new Set());
  const [bulkOpen, setBulkOpen] = useState(false);
  const { canWrite } = useCurrentUser();
  const agencyName = useAgencyLabel();
  const update = useUpdateRadio();
  const mode = useTheme();
  // The table needs `md`; below it each radio is a card.
  const tableFits = useMediaQuery("(min-width: 768px)");

  // Search reaches the whole corpus, not one page, so it goes to the server — debounced so
  // a keystroke does not fire a request per character.
  const [debouncedQuery, setDebouncedQuery] = useState("");
  useEffect(() => {
    const handle = setTimeout(() => setDebouncedQuery(query.trim()), 250);
    return () => clearTimeout(handle);
  }, [query]);

  const radios = useRadios({
    q: debouncedQuery || undefined,
    sort: `${descending ? "-" : ""}${API_SORT[sort]}`,
    activity: activity === "all" ? undefined : activity,
    page,
    page_size: PAGE_SIZE,
  });
  const stats = useRadioStatsList({}, STATS_LIMIT);
  // The same radio id can exist on two systems (5790004 does), so the row says which.
  const systems = useSystems();
  const systemName = useMemo(
    () => new Map((systems.data ?? []).map((system) => [system.id, system.name])),
    [systems.data],
  );
  const statsById = useMemo(
    () => new Map((stats.data ?? []).map((stat: RadioStat) => [stat.radio_id, stat])),
    [stats.data],
  );

  const rows = radios.data?.items ?? [];

  /**
   * Columns with nothing in them on this page, for a reader who cannot fill them in. A
   * guest saw Unit, Alias, Agency and Most used channel as four columns of dashes; an
   * operator keeps them, because an empty cell is where they type.
   */
  const emptyColumns = useMemo(() => {
    const empty = new Set<ColumnKey>();
    if (canWrite || rows.length === 0) return empty;
    if (rows.every((row) => !row.unit_id)) empty.add("unit");
    if (rows.every((row) => !row.alias)) empty.add("alias");
    if (rows.every((row) => !row.agency_code)) empty.add("agency");
    if (!stats.isLoading && rows.every((row) => !statsById.get(row.id)?.top_talkgroup)) empty.add("top");
    return empty;
  }, [canWrite, rows, stats.isLoading, statsById]);
  const visibleColumns = COLUMNS.filter(
    (column) => (canWrite || !column.write) && !emptyColumns.has(column.key),
  );
  const shows = (key: ColumnKey) => !emptyColumns.has(key);
  const total = radios.data?.total ?? 0;
  const pageIds = useMemo(() => rows.map((row) => row.id), [rows]);
  const pickedIds = useMemo(() => [...picked], [picked]);
  const checkedHere = pageIds.filter((id) => picked.has(id)).length;
  const allOnPageChecked = pageIds.length > 0 && checkedHere === pageIds.length;

  // A selection belongs to the question that produced it; changing the question
  // starts a fresh one.
  // biome-ignore lint/correctness/useExhaustiveDependencies: clear on the query only
  useEffect(() => {
    setPicked(new Set());
  }, [debouncedQuery, sort, descending, activity, page]);

  /** One field at a time; `exclude_unset` makes the server write exactly that column. */
  const patchField = (radio: Radio, patch: RadioPatch) => update.mutateAsync({ id: radio.id, patch });

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Radios"
        description="Every radio the system has seen — heard talking, or just registering on the control channel — which unit it is, and where it spends its time."
        actions={
          <>
            <Select
              value={activity}
              onValueChange={(value) => {
                setActivity(value as Activity);
                setPage(1);
              }}
              options={ACTIVITY_OPTIONS}
              tip={HELP["radios.activity"]}
              ariaLabel="Filter by activity"
              size="sm"
            />
            <Tip label={HELP["radios.search"]}>
              <input
                type="search"
                value={query}
                onChange={(event) => {
                  setQuery(event.target.value);
                  setPage(1);
                }}
                placeholder="Unit, alias or id…"
                aria-label="Search radios"
                className={`${inputClass} h-8 w-48`}
              />
            </Tip>
            {/* Hidden, not greyed out, for a read-only viewer. */}
            {canWrite ? (
              <Button tip={HELP["radios.import"]} size="sm" onClick={() => setImportOpen(true)}>
                <Upload size={14} aria-hidden="true" /> Import
              </Button>
            ) : null}
          </>
        }
      />

      {/* `relative` for the reason Talkgroups gives at length: the rows carry
          absolutely positioned `sr-only` labels, and without a positioned ancestor
          here they resolve against `<main>` and escape this box's clip, giving the
          window a second scrollbar over content it does not have. */}
      <div className="relative min-h-0 flex-1 overflow-auto scrollbar-thin px-4 pb-6 md:px-6">
        {radios.error ? (
          <ErrorState error={radios.error} onRetry={() => void radios.refetch()} />
        ) : radios.isLoading && !radios.data ? (
          <SkeletonText lines={10} />
        ) : rows.length === 0 ? (
          <EmptyState
            title="No radios match"
            hint={
              debouncedQuery || activity !== "all"
                ? "Clear the search box or set Activity back to All to see every radio."
                : "No radios have been seen yet."
            }
          />
        ) : !tableFits ? (
          // Below `md`, cards with the key fields (as `CallCards` does for calls); the
          // pencil opens the same editor the table's inline cells save through.
          <ul aria-label="Radios" className="divide-y divide-line">
            {rows.map((radio) => {
              const stat = statsById.get(radio.id);
              return (
                <li key={radio.id} className="flex items-start gap-2 py-2.5">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <Link
                        to={`/radios/${radio.id}`}
                        aria-label={`Open ${radioLabel(radio)}`}
                        className="block min-w-0 truncate text-sm font-medium text-ink hover:underline"
                      >
                        {radioLabel(radio)}
                      </Link>
                      <span className="ml-auto shrink-0 text-[11px] text-ink-2">
                        {formatRelative(radio.last_seen_at)}
                      </span>
                    </div>
                    <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[11px] text-ink-2">
                      {radio.unit_id || radio.alias ? (
                        <span className="font-mono tabular-nums">{radio.radio_id}</span>
                      ) : null}
                      <span>{systemName.get(radio.system_id) ?? "—"}</span>
                      <span>{radio.kind}</span>
                      {radio.agency_code ? <span>{agencyName(radio.agency_code)}</span> : null}
                      <span className="tabular-nums">
                        {radio.call_count === 0
                          ? "seen, no calls"
                          : `${formatNumber(radio.call_count)} calls`}
                      </span>
                      {stat?.top_talkgroup ? (
                        <span className="truncate">mostly {stat.top_talkgroup}</span>
                      ) : null}
                    </div>
                  </div>
                  {canWrite ? (
                    <Button
                      tip={HELP["radios.edit"]}
                      size="iconSm"
                      variant="ghost"
                      aria-label={`Edit radio ${radio.radio_id}`}
                      onClick={() => setEditing(radio)}
                    >
                      <Pencil size={14} aria-hidden="true" />
                    </Button>
                  ) : null}
                </li>
              );
            })}
          </ul>
        ) : (
          <table className="w-full min-w-[48rem] text-left text-sm">
            <caption className="sr-only">
              Radios with call counts, last-24-hour activity, the channel each uses most, and when it was last
              heard
            </caption>
            <thead className="sticky top-0 z-10 bg-page">
              <tr>
                {visibleColumns.map((column) => (
                  <th
                    key={column.key}
                    scope="col"
                    // Sentence case throughout: the sorting headers are buttons, which do
                    // not inherit `uppercase`, so the row used to mix "Unit" with "ALIAS".
                    className="border-b border-line px-2 py-2 text-[11px] font-semibold text-ink-2"
                  >
                    {column.key === "select" ? (
                      <SelectAllCheckbox
                        allChecked={allOnPageChecked}
                        someChecked={checkedHere > 0}
                        onChange={(on) => setPicked((current) => toggleInSet(current, pageIds, on))}
                      />
                    ) : !SORTABLE.has(column.key) ? (
                      column.label ? (
                        <Tip label={HELP[column.help]}>
                          <span tabIndex={0}>{column.label}</span>
                        </Tip>
                      ) : (
                        <span className="sr-only">Actions</span>
                      )
                    ) : (
                      <Tip label={`${HELP[column.help]}. ${HELP["calls.table.sort"]}`}>
                        <button
                          type="button"
                          aria-label={`Sort by ${column.label}`}
                          onClick={() => {
                            if (sort === column.key) setDescending(!descending);
                            else {
                              setSort(column.key as SortKey);
                              setDescending(true);
                            }
                            setPage(1);
                          }}
                          className={cn(
                            "inline-flex items-center gap-1 hover:text-ink",
                            sort === column.key && "text-accent-ink",
                          )}
                        >
                          {column.label}
                          {sort === column.key ? (
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
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((radio) => {
                const stat = statsById.get(radio.id);
                const silent = radio.call_count === 0;
                return (
                  <tr key={radio.id} className="border-b border-line hover:bg-surface-2">
                    {canWrite ? (
                      <td className="w-6 px-1 py-1.5">
                        <SelectRowCheckbox
                          checked={picked.has(radio.id)}
                          label={radioLabel(radio)}
                          onChange={(on) => setPicked((current) => toggleInSet(current, [radio.id], on))}
                        />
                      </td>
                    ) : null}
                    {shows("unit") ? (
                      <td className="min-w-40 px-2 py-1.5">
                        <InlineText
                          value={radio.unit_id}
                          onSave={(next) => patchField(radio, { unit_id: next })}
                          label={`Unit number for radio ${radio.radio_id}`}
                          tip={HELP["radios.edit.unit"]}
                          placeholder="E1"
                          hint="Assign a unit — the callsign this radio answers to, like E1 or 1201."
                          disabled={!canWrite}
                          emptyAsNull
                          className="max-w-40 font-medium text-ink"
                        />
                      </td>
                    ) : null}
                    {shows("alias") ? (
                      <td className="min-w-40 px-2 py-1.5">
                        <InlineText
                          value={radio.alias}
                          onSave={(next) => patchField(radio, { alias: next })}
                          label={`Alias for radio ${radio.radio_id}`}
                          tip={HELP["radios.edit.alias"]}
                          disabled={!canWrite}
                          emptyAsNull
                          className="max-w-48 text-ink-2"
                        />
                      </td>
                    ) : null}
                    {/* The id is the link to the radio's page: it used to be a separate word
                        "open" beside the unit, which said nothing about where it went. */}
                    <td className="px-2 py-1.5 font-mono text-xs tabular-nums">
                      <Link
                        to={`/radios/${radio.id}`}
                        aria-label={`Open ${radioLabel(radio)}`}
                        className="text-accent-ink hover:underline"
                      >
                        {radio.radio_id}
                      </Link>
                    </td>
                    <td className="max-w-48 truncate px-2 py-1.5 text-xs text-ink-2">
                      {systemName.get(radio.system_id) ?? "—"}
                    </td>
                    <td className="w-28 px-2 py-1.5">
                      {canWrite ? (
                        <InlineSelect
                          value={radio.kind}
                          onSave={(next) => patchField(radio, { kind: next as RadioKind })}
                          label={`Kind of radio ${radio.radio_id}`}
                          tip={HELP["radios.edit.kind"]}
                          options={RADIO_KINDS.map((kind) => ({ value: kind, label: kind }))}
                        />
                      ) : (
                        <span className="text-xs text-ink-2">{radio.kind}</span>
                      )}
                    </td>
                    {shows("agency") ? (
                      <td className="w-40 px-2 py-1.5">
                        {canWrite ? (
                          <AgencySelect
                            value={radio.agency_code}
                            onValueChange={(code) => {
                              void patchField(radio, { agency_code: code }).catch((error: unknown) =>
                                mutationErrorToast(error, "Could not save the agency"),
                              );
                            }}
                            tip={HELP["radios.edit.agency"]}
                            ariaLabel={`Agency for radio ${radio.radio_id}`}
                            size="sm"
                            className="h-7 w-full text-xs"
                          />
                        ) : (
                          <span className="text-xs text-ink-2">{agencyName(radio.agency_code)}</span>
                        )}
                      </td>
                    ) : null}
                    <td className="px-2 py-1.5 tabular-nums text-ink-2">
                      {silent ? (
                        <span className="inline-flex items-center gap-1.5">
                          <span>0</span>
                          <Tip label={HELP["radios.seen"]}>
                            <span
                              tabIndex={0}
                              className="rounded-full border border-line px-1.5 py-0.5 text-[11px] font-medium text-muted"
                            >
                              seen, no calls
                            </span>
                          </Tip>
                        </span>
                      ) : (
                        formatNumber(radio.call_count)
                      )}
                    </td>
                    <td className="px-2 py-1.5">
                      <Sparkline
                        values={stat?.spark ?? []}
                        loading={stats.isLoading}
                        color={seriesColor(String(radio.radio_id), mode)}
                        label={`Transmissions per hour over the last 24 hours`}
                      />
                    </td>
                    {shows("top") ? (
                      <td className="px-2 py-1.5 text-xs text-ink-2">
                        {stats.isLoading ? "…" : (stat?.top_talkgroup ?? "—")}
                      </td>
                    ) : null}
                    <td className="px-2 py-1.5 text-xs text-ink-2">{formatRelative(radio.last_seen_at)}</td>
                    {canWrite ? (
                      <td className="px-2 py-1.5 text-right">
                        <Button
                          tip={HELP["radios.edit"]}
                          size="iconSm"
                          variant="ghost"
                          aria-label={`Edit radio ${radio.radio_id}`}
                          onClick={() => setEditing(radio)}
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
            noun="radios"
            loadedCount={rows.length}
            canWrite={canWrite}
            atLimit={picked.size > BULK_MAX_IDS}
            onEdit={() => setBulkOpen(true)}
            onClear={() => setPicked(new Set())}
            onSelectAllLoaded={() => setPicked((current) => toggleInSet(current, pageIds, true))}
          />
        ) : null}

        {rows.length > 0 ? (
          <ListPager page={page} pageSize={PAGE_SIZE} total={total} onPageChange={setPage} noun="radios" />
        ) : null}
      </div>

      {canWrite ? (
        <>
          <BulkEditRadiosDrawer open={bulkOpen} onOpenChange={setBulkOpen} ids={pickedIds} />
          <EditRadioDialog
            radio={editing}
            open={editing !== null}
            onOpenChange={(open) => !open && setEditing(null)}
          />
          <ImportDialog kind="radios" open={importOpen} onOpenChange={setImportOpen} />
        </>
      ) : null}
    </div>
  );
}
