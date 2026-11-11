import { Download, Pencil, Plus, Upload } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { apiUrl } from "../api/client";
import { useAgencyLabel, useCurrentUser, usePersonnel, useUpdatePerson } from "../api/hooks";
import { BULK_MAX_IDS, type Person, type PersonPatch } from "../api/types";
import { AgencySelect } from "../components/directory/AgencySelect";
import { BulkEditPersonnelDrawer } from "../components/directory/BulkEditDrawer";
import { EditPersonnelDialog } from "../components/directory/EditPersonnelDialog";
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
import { InlineSwitch, InlineText } from "../components/ui/InlineCell";
import { ListPager } from "../components/ui/ListPager";
import { SkeletonText } from "../components/ui/Skeleton";
import { EmptyState, ErrorState } from "../components/ui/States";
import { Switch } from "../components/ui/Switch";
import { Tip } from "../components/ui/Tooltip";
import { HELP } from "../help/help";
import { formatNumber } from "../lib/format";
import { useMediaQuery } from "../lib/useMediaQuery";
import { mutationErrorToast } from "../state/toasts";

const PAGE_SIZE = 200;

const COLUMNS = [
  { key: "name", label: "Name", help: "roster.personnel.table.name" },
  { key: "rank", label: "Rank", help: "roster.personnel.table.rank" },
  { key: "badge", label: "Badge", help: "roster.personnel.table.badge" },
  { key: "agency", label: "Agency", help: "roster.personnel.table.agency" },
  { key: "active", label: "On roster", help: "roster.personnel.edit.active" },
  { key: "radios", label: "Radios", help: "roster.personnel.table.radios" },
] as const;

/**
 * Columns that are only a control. A reader without write access cannot use the roster
 * switch, so the column goes rather than standing there greyed out — the dimmed row and
 * the "On the roster" filter already say who is off it.
 */
const WRITE_ONLY_COLUMNS = new Set<(typeof COLUMNS)[number]["key"]>(["active"]);

/**
 * The personnel roster.
 *
 * Defaults to showing only people currently on the roster: the inactive ones are
 * kept because the archive is historical, but they are not who you are looking
 * for when you open this page.
 */
export function Personnel() {
  const [query, setQuery] = useState("");
  const [activeOnly, setActiveOnly] = useState(true);
  const [page, setPage] = useState(1);
  const [editing, setEditing] = useState<Person | null>(null);
  const [creating, setCreating] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [picked, setPicked] = useState<ReadonlySet<number>>(() => new Set());
  const [bulkOpen, setBulkOpen] = useState(false);
  const { canWrite } = useCurrentUser();
  const agencyName = useAgencyLabel();
  const update = useUpdatePerson();
  // The table needs `md`; below it each person is a card.
  const tableFits = useMediaQuery("(min-width: 768px)");

  const [debouncedQuery, setDebouncedQuery] = useState("");
  useEffect(() => {
    const handle = setTimeout(() => setDebouncedQuery(query.trim()), 250);
    return () => clearTimeout(handle);
  }, [query]);

  const personnel = usePersonnel({
    q: debouncedQuery || undefined,
    active: activeOnly ? true : undefined,
    page,
    page_size: PAGE_SIZE,
  });
  const rows = personnel.data?.items ?? [];
  const total = personnel.data?.total ?? 0;
  const pageIds = useMemo(() => rows.map((row) => row.id), [rows]);
  const pickedIds = useMemo(() => [...picked], [picked]);
  const checkedHere = pageIds.filter((id) => picked.has(id)).length;
  const allOnPageChecked = pageIds.length > 0 && checkedHere === pageIds.length;

  // biome-ignore lint/correctness/useExhaustiveDependencies: clear on the query only
  useEffect(() => {
    setPicked(new Set());
  }, [debouncedQuery, activeOnly, page]);

  /** One field; `exclude_unset` means the server writes exactly that column. */
  const patchField = (person: Person, patch: PersonPatch) => update.mutateAsync({ id: person.id, patch });

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Personnel"
        description="The people who carry the radios, and which radio each one is holding right now."
        actions={
          <>
            <Switch
              checked={activeOnly}
              onCheckedChange={(next) => {
                setActiveOnly(next);
                setPage(1);
              }}
              label="On the roster"
              tip={HELP["roster.personnel.active"]}
            />
            <Tip label={HELP["roster.personnel.search"]}>
              <input
                type="search"
                value={query}
                onChange={(event) => {
                  setQuery(event.target.value);
                  setPage(1);
                }}
                placeholder="Name, rank or badge…"
                aria-label="Search personnel"
                className={`${inputClass} h-8 w-48`}
              />
            </Tip>
            {/* Hidden, not greyed out, for a read-only viewer. The Export link stays:
                the download needs no write scope. */}
            {canWrite ? (
              <>
                <Button
                  tip={HELP["roster.personnel.new"]}
                  size="sm"
                  variant="primary"
                  onClick={() => setCreating(true)}
                >
                  <Plus size={14} aria-hidden="true" /> New person
                </Button>
                <Button tip={HELP["roster.personnel.import"]} size="sm" onClick={() => setImportOpen(true)}>
                  <Upload size={14} aria-hidden="true" /> Import
                </Button>
              </>
            ) : null}
            <Tip label={HELP["roster.personnel.export"]}>
              <a
                href={apiUrl("/personnel/export")}
                className="inline-flex h-8 items-center gap-1.5 rounded-md border border-line px-2.5 text-sm text-ink hover:bg-surface-2"
              >
                <Download size={14} aria-hidden="true" /> Export
              </a>
            </Tip>
          </>
        }
      />

      {/* `relative` for the reason Talkgroups gives at length: the rows carry
          absolutely positioned `sr-only` labels, and without a positioned ancestor
          here they resolve against `<main>` and escape this box's clip, giving the
          window a second scrollbar over content it does not have. */}
      <div className="relative min-h-0 flex-1 overflow-auto scrollbar-thin px-4 pb-6 md:px-6">
        {personnel.error ? (
          <ErrorState error={personnel.error} onRetry={() => void personnel.refetch()} />
        ) : personnel.isLoading && !personnel.data ? (
          <SkeletonText lines={10} />
        ) : rows.length === 0 ? (
          <EmptyState
            title="Nobody on the roster yet"
            hint={
              debouncedQuery
                ? "Nothing matches that search — clear the box to see everybody."
                : "Add a person, or import a roster CSV, and then assign a radio from that radio's page."
            }
          />
        ) : !tableFits ? (
          // Below `md`, cards with the key fields (as `CallCards` does for calls).
          <ul aria-label="Personnel" className="divide-y divide-line">
            {rows.map((person) => (
              <li key={person.id} className="flex items-start gap-2 py-2.5">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <Link
                      to={`/personnel/${person.id}`}
                      className="block min-w-0 truncate text-sm font-medium text-ink hover:underline"
                    >
                      {person.name}
                    </Link>
                    {person.active ? null : (
                      <span className="shrink-0 text-[11px] text-ink-2">off the roster</span>
                    )}
                  </div>
                  <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[11px] text-ink-2">
                    {person.rank ? <span>{person.rank}</span> : null}
                    {person.badge ? <span className="font-mono">#{person.badge}</span> : null}
                    {person.agency_code ? <span>{agencyName(person.agency_code)}</span> : null}
                    <span className="tabular-nums">
                      {formatNumber(person.radio_count)} {person.radio_count === 1 ? "radio" : "radios"}
                    </span>
                  </div>
                </div>
                {canWrite ? (
                  <Button
                    tip={HELP["roster.personnel.edit"]}
                    size="iconSm"
                    variant="ghost"
                    aria-label={`Edit ${person.name}`}
                    onClick={() => setEditing(person)}
                  >
                    <Pencil size={14} aria-hidden="true" />
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <table className="w-full min-w-[44rem] text-left text-sm">
            <caption className="sr-only">Personnel with rank, badge, agency and assigned radio count</caption>
            <thead className="sticky top-0 z-10 bg-page">
              <tr>
                {canWrite ? (
                  <th scope="col" className="border-b border-line px-1 py-2">
                    <SelectAllCheckbox
                      allChecked={allOnPageChecked}
                      someChecked={checkedHere > 0}
                      onChange={(on) => setPicked((current) => toggleInSet(current, pageIds, on))}
                    />
                  </th>
                ) : null}
                {COLUMNS.filter((column) => canWrite || !WRITE_ONLY_COLUMNS.has(column.key)).map((column) => (
                  <th
                    key={column.key}
                    scope="col"
                    className="border-b border-line px-2 py-2 text-xs font-medium text-ink-2"
                  >
                    <Tip label={HELP[column.help]}>
                      <span tabIndex={0}>{column.label}</span>
                    </Tip>
                  </th>
                ))}
                {canWrite ? (
                  <th scope="col" className="border-b border-line px-2 py-2">
                    <span className="sr-only">Actions</span>
                  </th>
                ) : null}
              </tr>
            </thead>
            <tbody>
              {rows.map((person) => (
                <tr
                  key={person.id}
                  // Not dimmed with opacity: that took every value in the row under 4.5:1 (axe,
                  // review batch 2). The row says it in words instead, beside the name.
                  className="border-b border-line hover:bg-surface-2"
                >
                  {canWrite ? (
                    <td className="w-6 px-1 py-1.5">
                      <SelectRowCheckbox
                        checked={picked.has(person.id)}
                        label={person.name}
                        onChange={(on) => setPicked((current) => toggleInSet(current, [person.id], on))}
                      />
                    </td>
                  ) : null}
                  <td className="min-w-48 px-2 py-1.5">
                    <span className="flex items-center gap-1.5">
                      <InlineText
                        value={person.name}
                        onSave={(next) => patchField(person, { name: next ?? "" })}
                        label={`Name for ${person.name}`}
                        tip={HELP["roster.personnel.edit.name"]}
                        placeholder="J. Smith"
                        hint="Name this person."
                        disabled={!canWrite}
                        className="max-w-56 font-medium text-ink"
                      />
                      <Tip label={HELP["roster.personnel.table.name"]}>
                        <Link
                          to={`/personnel/${person.id}`}
                          aria-label={`Open ${person.name}`}
                          className="shrink-0 text-[11px] text-accent-ink hover:underline"
                        >
                          open
                        </Link>
                      </Tip>
                      {person.active ? null : (
                        <span className="shrink-0 text-[11px] text-ink-2">off the roster</span>
                      )}
                    </span>
                  </td>
                  <td className="min-w-32 px-2 py-1.5">
                    <InlineText
                      value={person.rank}
                      onSave={(next) => patchField(person, { rank: next })}
                      label={`Rank for ${person.name}`}
                      tip={HELP["roster.personnel.edit.rank"]}
                      placeholder="Captain"
                      hint="Add a rank."
                      disabled={!canWrite}
                      emptyAsNull
                      className="max-w-40 text-ink-2"
                    />
                  </td>
                  <td className="min-w-24 px-2 py-1.5">
                    <InlineText
                      value={person.badge}
                      onSave={(next) => patchField(person, { badge: next })}
                      label={`Badge for ${person.name}`}
                      tip={HELP["roster.personnel.edit.badge"]}
                      disabled={!canWrite}
                      emptyAsNull
                      mono
                      className="max-w-28 text-ink-2"
                    />
                  </td>
                  <td className="w-40 px-2 py-1.5">
                    {canWrite ? (
                      <AgencySelect
                        value={person.agency_code}
                        onValueChange={(code) => {
                          void patchField(person, { agency_code: code }).catch((error: unknown) =>
                            mutationErrorToast(error, "Could not save the agency"),
                          );
                        }}
                        tip={HELP["roster.personnel.edit.agency"]}
                        ariaLabel={`Agency for ${person.name}`}
                        size="sm"
                        className="h-7 w-full text-xs"
                      />
                    ) : (
                      <span className="text-xs text-ink-2">{agencyName(person.agency_code)}</span>
                    )}
                  </td>
                  {canWrite ? (
                    <td className="px-2 py-1.5">
                      <InlineSwitch
                        value={person.active}
                        onSave={(next) => patchField(person, { active: next })}
                        label={`${person.name} is on the roster`}
                        tip={HELP["roster.personnel.edit.active"]}
                      />
                    </td>
                  ) : null}
                  <td className="px-2 py-1.5 tabular-nums text-ink-2">{formatNumber(person.radio_count)}</td>
                  {canWrite ? (
                    <td className="px-2 py-1.5 text-right">
                      <Button
                        tip={HELP["roster.personnel.edit"]}
                        size="iconSm"
                        variant="ghost"
                        aria-label={`Edit ${person.name}`}
                        onClick={() => setEditing(person)}
                      >
                        <Pencil size={14} aria-hidden="true" />
                      </Button>
                    </td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {canWrite ? (
          <SelectionBar
            count={picked.size}
            noun="people"
            loadedCount={rows.length}
            canWrite={canWrite}
            atLimit={picked.size > BULK_MAX_IDS}
            onEdit={() => setBulkOpen(true)}
            onClear={() => setPicked(new Set())}
            onSelectAllLoaded={() => setPicked((current) => toggleInSet(current, pageIds, true))}
          />
        ) : null}

        {rows.length > 0 ? (
          <ListPager page={page} pageSize={PAGE_SIZE} total={total} onPageChange={setPage} noun="people" />
        ) : null}
      </div>

      {canWrite ? (
        <>
          <BulkEditPersonnelDrawer open={bulkOpen} onOpenChange={setBulkOpen} ids={pickedIds} />
          <EditPersonnelDialog
            person={editing}
            open={editing !== null}
            onOpenChange={(open) => !open && setEditing(null)}
          />
          <EditPersonnelDialog person={null} open={creating} onOpenChange={setCreating} />
          <ImportDialog kind="personnel" open={importOpen} onOpenChange={setImportOpen} />
        </>
      ) : null}
    </div>
  );
}
