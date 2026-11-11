import { Download, Pencil, Plus, Upload } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { apiUrl } from "../api/client";
import { useAgencyLabel, useCurrentUser, useUnits, useUpdateUnit } from "../api/hooks";
import { BULK_MAX_IDS, UNIT_KINDS, type Unit, type UnitKind, type UnitPatch } from "../api/types";
import { AgencySelect } from "../components/directory/AgencySelect";
import { BulkEditUnitsDrawer } from "../components/directory/BulkEditDrawer";
import { EditUnitDialog } from "../components/directory/EditUnitDialog";
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
import { SkeletonText } from "../components/ui/Skeleton";
import { EmptyState, ErrorState } from "../components/ui/States";
import { Tip } from "../components/ui/Tooltip";
import { HELP } from "../help/help";
import { formatNumber, humanizeName } from "../lib/format";
import { useMediaQuery } from "../lib/useMediaQuery";
import { mutationErrorToast } from "../state/toasts";

const COLUMNS = [
  { key: "callsign", label: "Callsign", help: "roster.units.table.callsign" },
  { key: "name", label: "Name", help: "roster.units.table.name" },
  { key: "kind", label: "Kind", help: "roster.units.table.kind" },
  { key: "agency", label: "Agency", help: "roster.units.table.agency" },
  { key: "radios", label: "Radios", help: "roster.units.table.radios" },
] as const;

const PAGE_SIZE = 200;

/**
 * The unit roster: what each callsign on the air actually is.
 *
 * Sorted by callsign rather than by traffic, because this is a list somebody
 * maintains and looks things up in — the busiest-first ordering the radio
 * directory uses would make a known callsign hard to find.
 *
 * The name, kind and agency are edited in the row. The **callsign is not**: it is
 * the unique key this table is looked up by, a rename fans out onto every radio
 * assigned to the unit, and a collision comes back as a 409 that deserves a form
 * to fail in rather than a cell that silently reverts. It stays behind the pencil.
 */
export function Units() {
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [editing, setEditing] = useState<Unit | null>(null);
  const [creating, setCreating] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [picked, setPicked] = useState<ReadonlySet<number>>(() => new Set());
  const [bulkOpen, setBulkOpen] = useState(false);
  const { canWrite } = useCurrentUser();
  const agencyName = useAgencyLabel();
  const update = useUpdateUnit();
  // The table needs `md`; below it each unit is a card.
  const tableFits = useMediaQuery("(min-width: 768px)");

  const [debouncedQuery, setDebouncedQuery] = useState("");
  useEffect(() => {
    const handle = setTimeout(() => setDebouncedQuery(query.trim()), 250);
    return () => clearTimeout(handle);
  }, [query]);

  const units = useUnits({ q: debouncedQuery || undefined, page, page_size: PAGE_SIZE });
  const rows = units.data?.items ?? [];
  const total = units.data?.total ?? 0;
  const pageIds = useMemo(() => rows.map((row) => row.id), [rows]);
  const pickedIds = useMemo(() => [...picked], [picked]);
  const checkedHere = pageIds.filter((id) => picked.has(id)).length;
  const allOnPageChecked = pageIds.length > 0 && checkedHere === pageIds.length;

  // biome-ignore lint/correctness/useExhaustiveDependencies: clear on the query only
  useEffect(() => {
    setPicked(new Set());
  }, [debouncedQuery, page]);

  /** One field; `exclude_unset` means the server writes exactly that column. */
  const patchField = (unit: Unit, patch: UnitPatch) => update.mutateAsync({ id: unit.id, patch });

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Units"
        description="The apparatus, stations and crews a callsign names, and how many radios each one is carrying right now."
        actions={
          <>
            <Tip label={HELP["roster.units.search"]}>
              <input
                type="search"
                value={query}
                onChange={(event) => {
                  setQuery(event.target.value);
                  setPage(1);
                }}
                placeholder="Callsign or name…"
                aria-label="Search units"
                className={`${inputClass} h-8 w-48`}
              />
            </Tip>
            {/* Hidden outright from a read-only viewer rather than greyed out: see
                `Talkgroups`. The Export link stays — the download needs no write scope. */}
            {canWrite ? (
              <>
                <Button
                  tip={HELP["roster.units.new"]}
                  size="sm"
                  variant="primary"
                  onClick={() => setCreating(true)}
                >
                  <Plus size={14} aria-hidden="true" /> New unit
                </Button>
                <Button tip={HELP["roster.units.import"]} size="sm" onClick={() => setImportOpen(true)}>
                  <Upload size={14} aria-hidden="true" /> Import
                </Button>
              </>
            ) : null}
            <Tip label={HELP["roster.units.export"]}>
              <a
                href={apiUrl("/units/export")}
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
        {units.error ? (
          <ErrorState error={units.error} onRetry={() => void units.refetch()} />
        ) : units.isLoading && !units.data ? (
          <SkeletonText lines={10} />
        ) : rows.length === 0 ? (
          <EmptyState
            title="No units yet"
            hint={
              debouncedQuery
                ? "Nothing matches that search — clear the box to see the whole roster."
                : "Add a unit, or import a roster CSV, and then assign radios to it from a radio's page."
            }
          />
        ) : !tableFits ? (
          // Below `md`, cards with the key fields (as `CallCards` does for calls).
          <ul aria-label="Units" className="divide-y divide-line">
            {rows.map((unit) => (
              <li key={unit.id} className="flex items-start gap-2 py-2.5">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <Link
                      to={`/units/${unit.id}`}
                      className="block shrink-0 text-sm font-medium text-ink hover:underline"
                    >
                      {unit.callsign}
                    </Link>
                    {unit.name ? (
                      <span className="min-w-0 truncate text-sm text-ink-2">{unit.name}</span>
                    ) : null}
                  </div>
                  <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[11px] text-ink-2">
                    <span>{humanizeName(unit.kind)}</span>
                    {unit.agency_code ? <span>{agencyName(unit.agency_code)}</span> : null}
                    <span className="tabular-nums">
                      {formatNumber(unit.radio_count)} {unit.radio_count === 1 ? "radio" : "radios"}
                    </span>
                  </div>
                </div>
                {canWrite ? (
                  <Button
                    tip={HELP["roster.units.edit"]}
                    size="iconSm"
                    variant="ghost"
                    aria-label={`Edit unit ${unit.callsign}`}
                    onClick={() => setEditing(unit)}
                  >
                    <Pencil size={14} aria-hidden="true" />
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <table className="w-full min-w-[44rem] text-left text-sm">
            <caption className="sr-only">
              Units with their name, kind, agency and assigned radio count
            </caption>
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
                {COLUMNS.map((column) => (
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
              {rows.map((unit) => (
                <tr key={unit.id} className="border-b border-line hover:bg-surface-2">
                  {canWrite ? (
                    <td className="w-6 px-1 py-1.5">
                      <SelectRowCheckbox
                        checked={picked.has(unit.id)}
                        label={`unit ${unit.callsign}`}
                        onChange={(on) => setPicked((current) => toggleInSet(current, [unit.id], on))}
                      />
                    </td>
                  ) : null}
                  <td className="px-2 py-1.5">
                    <Link to={`/units/${unit.id}`} className="font-medium text-ink hover:underline">
                      {unit.callsign}
                    </Link>
                  </td>
                  <td className="min-w-48 px-2 py-1.5">
                    <InlineText
                      value={unit.name}
                      onSave={(next) => patchField(unit, { name: next })}
                      label={`Name for unit ${unit.callsign}`}
                      tip={HELP["roster.units.edit.name"]}
                      placeholder="Engine 1"
                      hint="Name this unit — what dispatch calls it in plain words."
                      disabled={!canWrite}
                      emptyAsNull
                      className="max-w-72 text-ink-2"
                    />
                  </td>
                  <td className="w-32 px-2 py-1.5">
                    {canWrite ? (
                      <InlineSelect
                        value={unit.kind}
                        onSave={(next) => patchField(unit, { kind: next as UnitKind })}
                        label={`Kind of unit ${unit.callsign}`}
                        tip={HELP["roster.units.edit.kind"]}
                        options={UNIT_KINDS.map((kind) => ({ value: kind, label: humanizeName(kind) }))}
                      />
                    ) : (
                      <span className="text-xs text-ink-2">{humanizeName(unit.kind)}</span>
                    )}
                  </td>
                  <td className="w-40 px-2 py-1.5">
                    {canWrite ? (
                      <AgencySelect
                        value={unit.agency_code}
                        onValueChange={(code) => {
                          void patchField(unit, { agency_code: code }).catch((error: unknown) =>
                            mutationErrorToast(error, "Could not save the agency"),
                          );
                        }}
                        tip={HELP["roster.units.edit.agency"]}
                        ariaLabel={`Agency for unit ${unit.callsign}`}
                        size="sm"
                        className="h-7 w-full text-xs"
                      />
                    ) : (
                      <span className="text-xs text-ink-2">{agencyName(unit.agency_code)}</span>
                    )}
                  </td>
                  <td className="px-2 py-1.5 tabular-nums text-ink-2">{formatNumber(unit.radio_count)}</td>
                  {canWrite ? (
                    <td className="px-2 py-1.5 text-right">
                      <Button
                        tip={HELP["roster.units.edit"]}
                        size="iconSm"
                        variant="ghost"
                        aria-label={`Edit unit ${unit.callsign}`}
                        onClick={() => setEditing(unit)}
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
            noun="units"
            loadedCount={rows.length}
            canWrite={canWrite}
            atLimit={picked.size > BULK_MAX_IDS}
            onEdit={() => setBulkOpen(true)}
            onClear={() => setPicked(new Set())}
            onSelectAllLoaded={() => setPicked((current) => toggleInSet(current, pageIds, true))}
          />
        ) : null}

        {rows.length > 0 ? (
          <ListPager page={page} pageSize={PAGE_SIZE} total={total} onPageChange={setPage} noun="units" />
        ) : null}
      </div>

      {canWrite ? (
        <>
          <BulkEditUnitsDrawer open={bulkOpen} onOpenChange={setBulkOpen} ids={pickedIds} />
          <EditUnitDialog
            unit={editing}
            open={editing !== null}
            onOpenChange={(open) => !open && setEditing(null)}
          />
          <EditUnitDialog unit={null} open={creating} onOpenChange={setCreating} />
          <ImportDialog kind="units" open={importOpen} onOpenChange={setImportOpen} />
        </>
      ) : null}
    </div>
  );
}
