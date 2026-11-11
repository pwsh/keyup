import { ChevronDown, GitMerge, Network, Pencil, RadioTower, Trash2 } from "lucide-react";
import { Fragment, useState } from "react";
import { useSearchParams } from "react-router";
import { errorDetail } from "../api/client";
import {
  useCurrentUser,
  useDeleteSite,
  useDeleteSystem,
  useOccupancy,
  useSites,
  useSystemChannelAudit,
  useSystems,
} from "../api/hooks";
import type { SiteInfo, SystemInfo } from "../api/types";
import { PageHeader } from "../components/layout/PageHeader";
import { BandStrip, BandStripLegend } from "../components/recorders/BandStrip";
import { ChannelAuditPanel } from "../components/sites/ChannelAuditPanel";
import { EditSiteDialog } from "../components/sites/EditSiteDialog";
import { EditSystemDialog } from "../components/sites/EditSystemDialog";
import { MergeSiteDialog } from "../components/sites/MergeSiteDialog";
import { MergeSystemDialog } from "../components/sites/MergeSystemDialog";
import { MessageKindsPanel } from "../components/sites/MessageKindsPanel";
import { SiteFrequenciesPanel } from "../components/sites/SiteFrequenciesPanel";
import { ServiceChip, SiteProfilePanel } from "../components/sites/SiteProfilePanel";
import { Button } from "../components/ui/Button";
import { Card, CardHeader } from "../components/ui/Card";
import { Dialog } from "../components/ui/Dialog";
import { SkeletonText } from "../components/ui/Skeleton";
import { EmptyState, ErrorState } from "../components/ui/States";
import { Tip } from "../components/ui/Tooltip";
import { HELP } from "../help/help";
import { cn } from "../lib/cn";
import { formatFrequency, formatNumber, formatRelative, siteLabel } from "../lib/format";
import { toast } from "../state/toasts";

/** What is holding a site or a system, spelled out for the tooltip on a disabled Delete. */
function blockedBy(row: { blocking?: Record<string, number> | null }): string {
  const parts = Object.entries(row.blocking ?? {}).map(
    ([table, rows]) => `${formatNumber(rows)} ${table.replace(/_/g, " ")}`,
  );
  return parts.length > 0 ? `Still referenced by ${parts.join(", ")}` : "";
}

/**
 * The systems that own the sites below — edit, merge, delete, and the RadioReference
 * mapping the channel audit has always asked for and never offered.
 *
 * It sits on this page rather than behind an Admin tab because a system is only ever
 * looked at through its sites: the reason to rename one is that its sites are filed under
 * a typo, and the reason to merge one is that half its sites are duplicates of the other
 * half's. Admin's tabs are the pipeline; this is the directory.
 */
function SystemsCard({
  canWrite,
  isAdmin,
  editing,
  setEditing,
}: {
  canWrite: boolean;
  isAdmin: boolean;
  /** Lifted, because the channel audit's "Map to RadioReference" opens this same dialog. */
  editing: SystemInfo | null;
  setEditing: (system: SystemInfo | null) => void;
}) {
  const systems = useSystems();
  const remove = useDeleteSystem();
  const [merging, setMerging] = useState<SystemInfo | null>(null);
  const [deleting, setDeleting] = useState<SystemInfo | null>(null);
  const rows = systems.data ?? [];
  // An archive with no systems has nothing to edit and no story to tell about it; the
  // page's own empty state already says why it is empty.
  if (rows.length === 0) return null;

  return (
    <Card>
      <CardHeader
        title={
          <span className="flex items-center gap-2">
            <Network size={15} aria-hidden="true" /> Radio systems
          </span>
        }
        hint={HELP["systems.table"]}
      />
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">
            Every radio system with its site and call counts, its RadioReference mapping and the actions that
            edit, merge or delete it
          </caption>
          <thead>
            <tr className="text-xs font-medium text-ink-2">
              <th scope="col" className="border-b border-line px-4 py-2">
                System
              </th>
              <th scope="col" className="border-b border-line px-2 py-2 text-right">
                Sites
              </th>
              <th scope="col" className="border-b border-line px-2 py-2 text-right">
                Calls
              </th>
              <th scope="col" className="border-b border-line px-2 py-2">
                RadioReference
              </th>
              <th scope="col" className="border-b border-line px-4 py-2 text-right">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((system) => (
              <tr key={system.id} className="border-b border-line last:border-b-0">
                <td className="px-4 py-2">
                  <span className="block font-medium text-ink">{system.name}</span>
                  {system.description ? (
                    <span className="block text-[11px] text-ink-2">{system.description}</span>
                  ) : null}
                </td>
                <td className="px-2 py-2 text-right tabular-nums text-ink-2">
                  {formatNumber(system.site_count)}
                </td>
                <td className="px-2 py-2 text-right tabular-nums text-ink-2">
                  {formatNumber(system.call_count)}
                </td>
                <td className="px-2 py-2 text-xs text-ink-2">
                  {system.rr_system_id === null
                    ? "not mapped"
                    : `sid ${system.rr_system_id}, synced ${formatRelative(system.rr_synced_at)}`}
                </td>
                <td className="px-4 py-2">
                  <div className="flex justify-end gap-1">
                    <Button
                      tip={canWrite ? HELP["systems.edit"] : HELP["app.readOnly"]}
                      size="iconSm"
                      variant="ghost"
                      disabled={!canWrite}
                      aria-label={`Edit ${system.name}`}
                      onClick={() => setEditing(system)}
                    >
                      <Pencil size={14} aria-hidden="true" />
                    </Button>
                    <Button
                      tip={isAdmin ? HELP["systems.merge"] : HELP["app.adminOnly"]}
                      size="iconSm"
                      variant="ghost"
                      disabled={!isAdmin || rows.length < 2}
                      aria-label={`Merge ${system.name}`}
                      onClick={() => setMerging(system)}
                    >
                      <GitMerge size={14} aria-hidden="true" />
                    </Button>
                    <Button
                      tip={
                        !isAdmin
                          ? HELP["app.adminOnly"]
                          : system.deletable
                            ? HELP["systems.delete"]
                            : `${HELP["systems.delete.blocked"]}. ${blockedBy(system)}`
                      }
                      size="iconSm"
                      variant="ghost"
                      disabled={!isAdmin || !system.deletable}
                      aria-label={`Delete ${system.name}`}
                      onClick={() => setDeleting(system)}
                    >
                      <Trash2 size={14} aria-hidden="true" />
                    </Button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <EditSystemDialog
        system={editing}
        open={editing !== null}
        canMapRr={isAdmin}
        onOpenChange={(open) => !open && setEditing(null)}
      />
      <MergeSystemDialog
        system={merging}
        systems={rows}
        open={merging !== null}
        onOpenChange={(open) => !open && setMerging(null)}
      />
      <Dialog
        open={deleting !== null}
        onOpenChange={(open) => !open && setDeleting(null)}
        title={deleting ? `Delete ${deleting.name}?` : "Delete system"}
        description="Nothing belongs to this system, so deleting it loses no recordings — but a recorder posting under its name will create it again."
        footer={
          <>
            <Button tip={HELP["systems.delete.cancel"]} size="sm" onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button
              tip={HELP["systems.delete.confirm"]}
              size="sm"
              variant="danger"
              disabled={remove.isPending}
              onClick={() => {
                if (!deleting) return;
                const name = deleting.name;
                remove.mutate(deleting.id, {
                  onSuccess: () => {
                    toast(`Deleted ${name}`, { tone: "success" });
                    setDeleting(null);
                  },
                  onError: (error) =>
                    toast("Could not delete the system", {
                      tone: "error",
                      body: errorDetail(error),
                    }),
                });
              }}
            >
              Delete system
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-2">
          {deleting
            ? `${deleting.name} holds no sites, talkgroups, radios or calls. To empty a system that does, merge it into another one rather than deleting it.`
            : null}
        </p>
      </Dialog>
    </Card>
  );
}

/**
 * The tower sites of every system, and the channel audit for the selected one:
 * what the site is on the air, what RadioReference documents, what the recorder
 * is configured to hunt, and every difference between the three
 * (docs/CHANNEL_AUDIT.md).
 *
 * Every row names its **system** as well as its site. A site name on its own does
 * not identify a site — `sites` is unique on `(system_id, name)`, so two systems
 * can both have a `Springfield`, and production has exactly that — so the system is a
 * muted line under the name here and `siteLabel` is used wherever there is only
 * room for one line.
 *
 * The selection lives in the URL (`?site=`) so an alert can link straight to the
 * site it is about.
 */
export function Sites() {
  const [params, setParams] = useSearchParams();
  const sites = useSites();
  const systems = useSystems();
  const [editing, setEditing] = useState<SiteInfo | null>(null);
  const [editingSystem, setEditingSystem] = useState<SystemInfo | null>(null);
  const [merging, setMerging] = useState<SiteInfo | null>(null);
  const [deleting, setDeleting] = useState<SiteInfo | null>(null);
  // One row open at a time: the profile is a page of its own, and two of them open at
  // once turns the table into a scroll no one can find their way back up.
  const [expanded, setExpanded] = useState<number | null>(null);
  const remove = useDeleteSite();

  const rows = sites.data ?? [];
  const requested = Number(params.get("site"));
  const selected = rows.some((site) => site.id === requested) ? requested : (rows[0]?.id ?? null);
  const current = rows.find((site) => site.id === selected) ?? null;
  const summary = useSystemChannelAudit(current?.system_id ?? null);
  const counts = new Map((summary.data?.sites ?? []).map((row) => [row.site_id, row]));
  const { canWrite, isAdmin } = useCurrentUser();
  // Occupancy is admin-gated on the API, so only fetch it for admins (avoids a 403 for others).
  const occupancy = useOccupancy(selected, isAdmin);
  const strip = (occupancy.data ?? []).find((row) => row.site_id === selected) ?? null;

  if (sites.isLoading) return <SkeletonText lines={8} className="p-6" />;
  if (sites.error) return <ErrorState error={sites.error} onRetry={() => void sites.refetch()} />;

  const systemName = current?.system_name ?? null;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Sites"
        description="Every tower site the recorder has heard, and how what it announces on the air compares with RadioReference and with the recorder's own configuration."
      />

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto scrollbar-thin p-4 pb-40 md:p-6">
        <SystemsCard
          canWrite={canWrite}
          isAdmin={isAdmin}
          editing={editingSystem}
          setEditing={setEditingSystem}
        />

        {rows.length === 0 ? (
          <EmptyState
            title="No sites yet"
            hint="A site appears here once the recorder has posted a call or an observation for it."
            icon={<RadioTower size={28} />}
          />
        ) : (
          <>
            <Card>
              <CardHeader
                title="Tower sites"
                hint={
                  summary.data
                    ? `${formatNumber(summary.data.totals.undocumented ?? 0)} undocumented and ${formatNumber(summary.data.totals.missing ?? 0)} missing channels across ${systemName ?? "this system"}.`
                    : "Pick a site to audit its channels."
                }
              />
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <caption className="sr-only">
                    Every tower site with its system, call count, audit findings and the actions that edit,
                    merge or delete it
                  </caption>
                  <thead>
                    <tr className="text-xs font-medium text-ink-2">
                      <th scope="col" className="border-b border-line px-2 py-2">
                        <span className="sr-only">Details</span>
                      </th>
                      <th scope="col" className="border-b border-line px-4 py-2">
                        Site
                      </th>
                      <th scope="col" className="border-b border-line px-2 py-2 text-right">
                        Site #
                      </th>
                      <th scope="col" className="border-b border-line px-2 py-2">
                        Services
                      </th>
                      <th scope="col" className="border-b border-line px-2 py-2 text-right">
                        Calls
                      </th>
                      <th scope="col" className="border-b border-line px-2 py-2 text-right">
                        Findings
                      </th>
                      <th scope="col" className="border-b border-line px-4 py-2 text-right">
                        <span className="sr-only">Actions</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((site) => {
                      const row = counts.get(site.id);
                      const findings =
                        (row?.undocumented ?? 0) + (row?.missing ?? 0) + (row?.control_drift ?? 0);
                      const blocking = blockedBy(site);
                      const open = expanded === site.id;
                      const services = site.services_available;
                      return (
                        <Fragment key={site.id}>
                          <tr
                            aria-selected={site.id === selected}
                            className={cn(
                              site.id === selected ? "bg-accent-soft/40" : "hover:bg-surface-2",
                              open ? "" : "border-b border-line",
                            )}
                          >
                            <td className="px-2 py-2">
                              <Tip label={HELP["sites.profile.expand"]}>
                                <button
                                  type="button"
                                  aria-expanded={open}
                                  aria-controls={`site-profile-${site.id}`}
                                  aria-label={`${open ? "Hide" : "Show"} everything known about ${siteLabel(site)}`}
                                  onClick={() => setExpanded(open ? null : site.id)}
                                  className="rounded p-1 text-ink-2 hover:bg-surface-2 hover:text-ink"
                                >
                                  <ChevronDown
                                    size={14}
                                    aria-hidden="true"
                                    className={cn("transition-transform", open ? "" : "-rotate-90")}
                                  />
                                </button>
                              </Tip>
                            </td>
                            <td className="px-4 py-2">
                              <Tip label={HELP["sites.pick"]}>
                                <button
                                  type="button"
                                  aria-pressed={site.id === selected}
                                  // Named "System / Site" even though both are on screen:
                                  // the visible text reads as two lines, and a screen
                                  // reader would otherwise announce two identical buttons.
                                  aria-label={`Audit ${siteLabel(site)}`}
                                  onClick={() => setParams({ site: String(site.id) })}
                                  className="text-left"
                                >
                                  <span className="block font-medium text-ink">{site.name}</span>
                                  {/* The system, always: two systems can both have a "Springfield". */}
                                  <span className="block text-[11px] text-ink-2">
                                    {site.system_name ?? "—"}
                                  </span>
                                </button>
                              </Tip>
                            </td>
                            <td className="px-2 py-2 text-right tabular-nums text-ink-2">
                              {site.site_number ?? "—"}
                            </td>
                            <td className="px-2 py-2">
                              {/* Null is "nobody has decoded the service message here" — a
                                different answer from a site that offers nothing, and the
                                wording says which. */}
                              {services === null ? (
                                <Tip label={HELP["sites.profile.services"]}>
                                  <span tabIndex={0} className="text-[11px] text-muted">
                                    none decoded yet
                                  </span>
                                </Tip>
                              ) : (
                                <Tip label={HELP["sites.profile.services"]}>
                                  <span tabIndex={0} className="flex flex-wrap gap-1">
                                    {services.slice(0, 3).map((name) => (
                                      <ServiceChip key={name} name={name} />
                                    ))}
                                    {services.length > 3 ? (
                                      <span className="text-[11px] text-ink-2">+{services.length - 3}</span>
                                    ) : null}
                                  </span>
                                </Tip>
                              )}
                            </td>
                            <td className="px-2 py-2 text-right tabular-nums text-ink-2">
                              {formatNumber(site.call_count)}
                            </td>
                            <td className="px-2 py-2 text-right tabular-nums text-ink-2">
                              {findings > 0 ? findings : "—"}
                            </td>
                            <td className="px-4 py-2">
                              <div className="flex justify-end gap-1">
                                <Button
                                  tip={canWrite ? HELP["sites.edit"] : HELP["app.readOnly"]}
                                  size="iconSm"
                                  variant="ghost"
                                  disabled={!canWrite}
                                  aria-label={`Edit ${siteLabel(site)}`}
                                  onClick={() => setEditing(site)}
                                >
                                  <Pencil size={14} aria-hidden="true" />
                                </Button>
                                <Button
                                  tip={isAdmin ? HELP["sites.merge"] : HELP["app.adminOnly"]}
                                  size="iconSm"
                                  variant="ghost"
                                  disabled={!isAdmin}
                                  aria-label={`Merge ${siteLabel(site)}`}
                                  onClick={() => setMerging(site)}
                                >
                                  <GitMerge size={14} aria-hidden="true" />
                                </Button>
                                <Button
                                  tip={
                                    !isAdmin
                                      ? HELP["app.adminOnly"]
                                      : site.deletable
                                        ? HELP["sites.delete"]
                                        : `${HELP["sites.delete.blocked"]}. ${blocking}`
                                  }
                                  size="iconSm"
                                  variant="ghost"
                                  disabled={!isAdmin || !site.deletable}
                                  aria-label={`Delete ${siteLabel(site)}`}
                                  onClick={() => setDeleting(site)}
                                >
                                  <Trash2 size={14} aria-hidden="true" />
                                </Button>
                              </div>
                            </td>
                          </tr>
                          {open ? (
                            <tr className="border-b border-line bg-surface-2/40">
                              <td colSpan={7} id={`site-profile-${site.id}`} className="p-0">
                                <SiteProfilePanel
                                  siteId={site.id}
                                  onShowMessageKinds={() => setParams({ site: String(site.id) })}
                                />
                              </td>
                            </tr>
                          ) : null}
                        </Fragment>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {current ? (
                <dl className="grid grid-cols-2 gap-x-4 gap-y-2 border-t border-line px-4 py-3 text-xs sm:grid-cols-4">
                  <div>
                    <dt className="text-ink-2">System</dt>
                    <dd className="text-ink">{systemName ?? "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-ink-2">RFSS / site</dt>
                    <dd className="tabular-nums text-ink">
                      {current.rfss ?? "—"} / {current.site_number ?? "—"}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-ink-2">NAC</dt>
                    <dd className="tabular-nums text-ink">
                      {current.nac === null ? "—" : `0x${current.nac.toString(16).toUpperCase()}`}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-ink-2">Control channel</dt>
                    <dd className="tabular-nums text-ink">
                      {formatFrequency(counts.get(current.id)?.control_observed ?? null)}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-ink-2">RadioReference</dt>
                    <dd className="text-ink">
                      {current.rr_site_id === null
                        ? "not mapped"
                        : `site ${current.rr_site_id}, synced ${formatRelative(current.rr_synced_at)}`}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-ink-2">Last observed</dt>
                    <dd className="text-ink">
                      {formatRelative(counts.get(current.id)?.last_observed_at ?? null)}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-ink-2">Calls recorded</dt>
                    <dd className="tabular-nums text-ink">{formatNumber(current.call_count)}</dd>
                  </div>
                  <div>
                    <dt className="text-ink-2">Unknown neighbors</dt>
                    <dd className="tabular-nums text-ink">
                      {formatNumber(counts.get(current.id)?.adjacent_unknown ?? 0)}
                    </dd>
                  </div>
                  {current.notes ? (
                    <div className="col-span-2 sm:col-span-4">
                      <dt className="text-ink-2">Notes</dt>
                      <dd className="whitespace-pre-wrap text-ink">{current.notes}</dd>
                    </div>
                  ) : null}
                </dl>
              ) : null}
              {strip && strip.channels.length > 0 ? (
                <div className="border-t border-line px-4 py-3">
                  <div className="mb-1 flex flex-wrap items-center justify-between gap-2 text-xs">
                    <span className="font-medium text-ink">Band occupancy</span>
                    <BandStripLegend />
                  </div>
                  <BandStrip occupancy={strip} />
                </div>
              ) : null}
            </Card>

            <SiteFrequenciesPanel siteId={selected} />

            <MessageKindsPanel siteId={selected} />

            <ChannelAuditPanel
              siteId={selected}
              onMapRadioReference={
                canWrite && current
                  ? () =>
                      setEditingSystem(
                        (systems.data ?? []).find((row) => row.id === current.system_id) ?? null,
                      )
                  : undefined
              }
            />
          </>
        )}
      </div>

      <EditSiteDialog
        site={editing}
        open={editing !== null}
        onOpenChange={(open) => !open && setEditing(null)}
      />
      <MergeSiteDialog
        site={merging}
        sites={rows}
        open={merging !== null}
        onOpenChange={(open) => !open && setMerging(null)}
      />
      <Dialog
        open={deleting !== null}
        onOpenChange={(open) => !open && setDeleting(null)}
        title={deleting ? `Delete ${siteLabel(deleting)}?` : "Delete site"}
        description="Nothing references this site, so deleting it loses no recordings — but the site itself is gone, and the recorder will create it again the next time it hears one."
        footer={
          <>
            <Button tip={HELP["sites.delete.cancel"]} size="sm" onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button
              tip={HELP["sites.delete.confirm"]}
              size="sm"
              variant="danger"
              disabled={remove.isPending}
              onClick={() => {
                if (!deleting) return;
                const label = siteLabel(deleting);
                remove.mutate(deleting.id, {
                  onSuccess: () => {
                    toast(`Deleted ${label}`, { tone: "success" });
                    setDeleting(null);
                  },
                  onError: (error) =>
                    toast("Could not delete the site", {
                      tone: "error",
                      body: errorDetail(error),
                    }),
                });
              }}
            >
              Delete site
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-2">
          {deleting
            ? `${siteLabel(deleting)} holds no calls and nothing else names it. This cannot be undone.`
            : null}
        </p>
      </Dialog>
    </div>
  );
}
