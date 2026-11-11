/**
 * Admin → Agencies: the agency table.
 *
 * The reference data every talkgroup, radio, unit and person points at, and the
 * one part of it an operator has to be able to extend — the seeded list is
 * the dispatch system's own codes, and this receiver hears a good deal that
 * belongs to none of them.
 *
 * The row *is* the edit, the way Talkgroups and Radios already work: name, kind
 * and active are cells that save themselves, because renaming twenty agencies
 * through a modal is twenty modals. The pencil stays for the fuller form — the
 * short name, and the fields a dialog can explain — and for the one field this
 * table deliberately does not offer inline: a seeded agency's name, which
 * `keyup seed-agencies` owns the spelling of until an edit stamps `edited_at`.
 */

import { Pencil, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { useAdminAgencies, useDeleteAgency, useUpdateAgency } from "../../api/hooks";
import { type AdminAgency, AGENCY_KINDS, type AgencyPatch } from "../../api/types";
import { AgencyDialog } from "../../components/directory/AgencyDialog";
import { Button } from "../../components/ui/Button";
import { Card, CardHeader } from "../../components/ui/Card";
import { Chip } from "../../components/ui/Chip";
import { Dialog } from "../../components/ui/Dialog";
import { InlineSelect, InlineSwitch, InlineText } from "../../components/ui/InlineCell";
import { SkeletonText } from "../../components/ui/Skeleton";
import { EmptyState } from "../../components/ui/States";
import { Tip } from "../../components/ui/Tooltip";
import { HELP } from "../../help/help";
import { mutationErrorToast, toast } from "../../state/toasts";

/** How many rows point at this agency; zero across the board is what a delete needs. */
export const mappedCount = (agency: AdminAgency): number =>
  agency.talkgroup_count + agency.radio_count + agency.unit_count + agency.person_count;

/** Why the delete button is off, or null when it is on. */
export function deleteBlocker(agency: AdminAgency): string | null {
  if (agency.source !== "local") {
    return "This one comes from the seeded agency list and the next re-seed would bring it back. Make it inactive instead.";
  }
  const mapped = mappedCount(agency);
  if (mapped > 0) {
    return `${mapped} talkgroups, radios, units or people are still mapped to it. Reassign them, or make it inactive instead.`;
  }
  return null;
}

function SourceBadge({ agency }: { agency: AdminAgency }) {
  const local = agency.source === "local";
  return (
    <Chip
      label={local ? "local" : "seeded"}
      active={local}
      onClick={() => {}}
      tip={
        local
          ? HELP["admin.agencies.source.local"]
          : agency.edited_at
            ? HELP["admin.agencies.source.seedEdited"]
            : HELP["admin.agencies.source.seed"]
      }
    />
  );
}

/**
 * The kinds the picker offers for one row.
 *
 * `AGENCY_KINDS` is the API's vocabulary, but a row can be carrying something
 * outside it — an older database, or a kind added on the server before this
 * build shipped. Adding the row's own value keeps the select showing what the
 * agency actually is instead of silently reading as the first entry, which the
 * next blur would then save.
 */
function kindOptions(kind: string): { value: string; label: string }[] {
  const kinds: string[] = AGENCY_KINDS.includes(kind as (typeof AGENCY_KINDS)[number])
    ? [...AGENCY_KINDS]
    : [...AGENCY_KINDS, kind];
  return kinds.map((value) => ({ value, label: value }));
}

export function AgenciesTab({ canWrite }: { canWrite: boolean }) {
  const agencies = useAdminAgencies();
  const update = useUpdateAgency();
  const remove = useDeleteAgency();
  const [editing, setEditing] = useState<AdminAgency | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [doomed, setDoomed] = useState<AdminAgency | null>(null);

  const rows = agencies.data ?? [];

  const openAdd = () => {
    setEditing(null);
    setDialogOpen(true);
  };
  const openEdit = (agency: AdminAgency) => {
    setEditing(agency);
    setDialogOpen(true);
  };

  /**
   * Send one field.
   *
   * `exclude_unset` on the server means a body carrying a single key writes a
   * single column, so two people editing different columns of the same row do
   * not overwrite each other. The promise is handed straight back to the cell:
   * a rejection belongs under the cell that caused it, not in a toast that has
   * scrolled away from whichever of forty rows refused.
   */
  const patchField = (agency: AdminAgency, patch: AgencyPatch) =>
    update.mutateAsync({ code: agency.code, patch });

  return (
    <Card>
      <CardHeader
        title="Agencies"
        hint="Who owns a channel. Name, kind and active are edited in the row; the pencil opens the fuller form. The seeded rows come from the agency list keyup seed-agencies loads and refreshes; local rows are yours, and a re-seed never touches them."
        actions={
          canWrite ? (
            <Button tip={HELP["admin.agencies.add"]} size="sm" variant="primary" onClick={openAdd}>
              <Plus size={14} aria-hidden="true" />
              New agency
            </Button>
          ) : null
        }
      />

      {agencies.isPending ? (
        <div className="p-4">
          <SkeletonText lines={5} />
        </div>
      ) : rows.length === 0 ? (
        <EmptyState
          title="No agencies"
          hint="Run `keyup seed-agencies --csv <file>` to load an agency list, or add one here."
        />
      ) : (
        <div className="overflow-x-auto scrollbar-thin">
          <table className="w-full min-w-[44rem] text-left text-xs">
            <caption className="sr-only">
              Every agency with its source, whether it is active, and what is mapped to it
            </caption>
            <thead>
              <tr className="text-xs text-ink-2">
                <th scope="col" className="px-3 py-1.5">
                  Code
                </th>
                <th scope="col" className="px-3 py-1.5">
                  Name
                </th>
                <th scope="col" className="px-3 py-1.5">
                  Kind
                </th>
                <th scope="col" className="px-3 py-1.5">
                  Source
                </th>
                <th scope="col" className="px-3 py-1.5">
                  Active
                </th>
                <th scope="col" className="px-3 py-1.5 text-right">
                  Talkgroups
                </th>
                <th scope="col" className="px-3 py-1.5 text-right">
                  Radios
                </th>
                <th scope="col" className="px-3 py-1.5">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((agency) => {
                const blocker = deleteBlocker(agency);
                return (
                  <tr key={agency.code} className="border-t border-line">
                    <th scope="row" className="px-3 py-1.5 text-left font-mono font-normal text-ink">
                      {agency.code}
                    </th>
                    <td className="min-w-56 px-3 py-1.5 text-ink">
                      <span className="flex items-center gap-1.5">
                        {canWrite && agency.source === "local" ? (
                          <InlineText
                            value={agency.name}
                            onSave={(next) => {
                              const name = (next ?? "").trim();
                              // `name` is a NOT NULL column and the string every
                              // picker shows; an emptied cell is a slip, not an
                              // instruction, and saying so here beats a row that
                              // has gone blank everywhere else in the app.
                              if (name === "") {
                                return Promise.reject(new Error("An agency needs a name"));
                              }
                              return patchField(agency, { name });
                            }}
                            label={`Name for ${agency.code}`}
                            tip={HELP["admin.agencies.name"]}
                            className="max-w-[18rem] font-medium text-ink"
                          />
                        ) : (
                          /* A seeded name is the seeded list's spelling, and the
                             pencil is deliberately the only way to change it: the
                             dialog is where the consequence — the row is stamped and
                             the next re-seed stops refreshing it — can be said in a
                             sentence. */
                          <Tip
                            label={
                              agency.source === "local"
                                ? HELP["admin.agencies.name"]
                                : HELP["admin.agencies.name.seed"]
                            }
                          >
                            <span tabIndex={0} className="truncate font-medium text-ink">
                              {agency.name}
                            </span>
                          </Tip>
                        )}
                        <span className="shrink-0 text-muted">{agency.short_name}</span>
                      </span>
                    </td>
                    <td className="w-32 px-3 py-1.5 text-ink-2">
                      {canWrite ? (
                        <InlineSelect
                          value={agency.kind}
                          onSave={(next) => patchField(agency, { kind: next })}
                          label={`Kind for ${agency.code}`}
                          tip={HELP["admin.agencies.kind"]}
                          options={kindOptions(agency.kind)}
                        />
                      ) : (
                        agency.kind
                      )}
                    </td>
                    <td className="px-3 py-1.5">
                      <SourceBadge agency={agency} />
                    </td>
                    <td className="px-3 py-1.5 text-ink-2">
                      {canWrite ? (
                        <InlineSwitch
                          // `active` is optional in the schema with a default of
                          // true, and a switch has no third state to render.
                          value={agency.active ?? true}
                          onSave={(next) => patchField(agency, { active: next })}
                          label={`Active for ${agency.code}`}
                          tip={HELP["admin.agencies.active"]}
                        />
                      ) : agency.active ? (
                        "yes"
                      ) : (
                        <Tip label={HELP["admin.agencies.inactive"]}>
                          <span className="text-muted">inactive</span>
                        </Tip>
                      )}
                    </td>
                    <td className="px-3 py-1.5 text-right tabular-nums text-ink-2">
                      {agency.talkgroup_count}
                    </td>
                    <td className="px-3 py-1.5 text-right tabular-nums text-ink-2">{agency.radio_count}</td>
                    <td className="px-3 py-1.5">
                      {canWrite ? (
                        <div className="flex justify-end gap-1">
                          <Button
                            tip={HELP["admin.agencies.edit"]}
                            size="sm"
                            variant="ghost"
                            onClick={() => openEdit(agency)}
                            aria-label={`Edit ${agency.code}`}
                          >
                            <Pencil size={13} aria-hidden="true" />
                          </Button>
                          <Button
                            tip={blocker ?? HELP["admin.agencies.delete"]}
                            size="sm"
                            variant="ghost"
                            disabled={blocker !== null}
                            onClick={() => setDoomed(agency)}
                            aria-label={`Delete ${agency.code}`}
                          >
                            <Trash2 size={13} aria-hidden="true" />
                          </Button>
                        </div>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <AgencyDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        agency={editing}
        existingCodes={rows.map((agency) => agency.code)}
      />

      <Dialog
        open={doomed !== null}
        onOpenChange={(next) => !next && setDoomed(null)}
        title={doomed ? `Delete ${doomed.code}?` : "Delete agency"}
        description="Nothing is mapped to it, so nothing else changes. Adding it back later means retyping the code."
        footer={
          <>
            <Button tip="Keep the agency" size="sm" onClick={() => setDoomed(null)}>
              Cancel
            </Button>
            <Button
              tip={HELP["admin.agencies.delete.confirm"]}
              size="sm"
              variant="danger"
              disabled={remove.isPending}
              onClick={() => {
                if (!doomed) return;
                const code = doomed.code;
                remove.mutate(code, {
                  onSuccess: () => {
                    toast(`Agency ${code} deleted`, { tone: "success" });
                    setDoomed(null);
                  },
                  onError: (error) => mutationErrorToast(error, "Could not delete the agency"),
                });
              }}
            >
              Delete
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-2">
          {doomed
            ? `${doomed.name} was added here rather than coming from the seeded agency list, and no talkgroup, radio, unit or person points at it.`
            : ""}
        </p>
      </Dialog>
    </Card>
  );
}
