import { useCurrentUser, useSaveSiteFrequency, useSiteFrequencies } from "../../api/hooks";
import { HELP } from "../../help/help";
import { formatFrequency, formatNumber, formatRelative } from "../../lib/format";
import { mutationErrorToast } from "../../state/toasts";
import { Card, CardHeader } from "../ui/Card";
import { SkeletonText } from "../ui/Skeleton";
import { EmptyState } from "../ui/States";
import { Switch } from "../ui/Switch";

/**
 * The frequencies catalogued at one site, and the two export checkboxes on each.
 *
 * The checkboxes are the conventional-channel twin of the ones on the talkgroup grid:
 * a call with no talkgroup is keyed by the frequency it was heard on, so that is what
 * an operator has to be able to select. Like the talkgroup ones they are read only
 * when the service's talkgroup selection is `allow` or `deny` (Admin → Exports), and
 * both services ship switched off — so ticking or clearing one here publishes nothing
 * by itself.
 *
 * Everything else in the row is *observed* — the frequency, its role, which source
 * knows about it, the counters — and is deliberately not editable: editing it would
 * only make the channel audit disagree with the air.
 */
export function SiteFrequenciesPanel({ siteId }: { siteId: number | null }) {
  const { canWrite } = useCurrentUser();
  const frequencies = useSiteFrequencies(siteId);
  const save = useSaveSiteFrequency(siteId);
  const rows = frequencies.data ?? [];

  return (
    <Card>
      <CardHeader title="Frequencies" hint={HELP["sites.frequencies"]} />
      {frequencies.isLoading ? (
        <SkeletonText lines={5} className="p-4" />
      ) : rows.length === 0 ? (
        <EmptyState
          title="No frequencies catalogued"
          hint="Nothing has been observed at this site, and RadioReference has not been synced for it."
        />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">
              Frequencies at this site with their role and source, and whether calls heard on each with no
              talkgroup may be re-published to Broadcastify Calls or OpenMHz.
            </caption>
            <thead>
              <tr className="text-xs text-ink-2">
                <th scope="col" className="border-b border-line px-3 py-2 font-semibold">
                  Frequency
                </th>
                <th scope="col" className="border-b border-line px-3 py-2 font-semibold">
                  Role
                </th>
                <th scope="col" className="border-b border-line px-3 py-2 font-semibold">
                  Source
                </th>
                <th scope="col" className="border-b border-line px-3 py-2 text-right font-semibold">
                  Grants
                </th>
                <th scope="col" className="border-b border-line px-3 py-2 font-semibold">
                  Last heard
                </th>
                <th scope="col" className="border-b border-line px-3 py-2 font-semibold">
                  BCFY
                </th>
                <th scope="col" className="border-b border-line px-3 py-2 font-semibold">
                  OpenMHz
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className="border-b border-line/60">
                  <td className="px-3 py-1.5 tabular-nums text-ink">{formatFrequency(row.frequency_hz)}</td>
                  <td className="px-3 py-1.5 text-ink-2">{row.role.replace(/_/g, " ")}</td>
                  <td className="px-3 py-1.5 text-ink-2">{row.source}</td>
                  <td className="px-3 py-1.5 text-right tabular-nums text-ink-2">
                    {formatNumber(row.grant_count)}
                  </td>
                  <td className="px-3 py-1.5 text-ink-2">{formatRelative(row.last_seen_at)}</td>
                  <td className="px-3 py-1.5">
                    <Switch
                      checked={row.export_broadcastify}
                      disabled={!canWrite || save.isPending}
                      label=""
                      tip={HELP["sites.frequencies.exportBroadcastify"]}
                      onCheckedChange={(next) =>
                        save.mutate(
                          { id: row.id, patch: { export_broadcastify: next } },
                          {
                            onError: (error) =>
                              mutationErrorToast(error, "Could not change that export flag"),
                          },
                        )
                      }
                    />
                  </td>
                  <td className="px-3 py-1.5">
                    <Switch
                      checked={row.export_openmhz}
                      disabled={!canWrite || save.isPending}
                      label=""
                      tip={HELP["sites.frequencies.exportOpenmhz"]}
                      onCheckedChange={(next) =>
                        save.mutate(
                          { id: row.id, patch: { export_openmhz: next } },
                          {
                            onError: (error) =>
                              mutationErrorToast(error, "Could not change that export flag"),
                          },
                        )
                      }
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
