import { ArrowLeft, Pencil } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { useCurrentUser, useUnit } from "../api/hooks";
import { AssignedRadiosCard } from "../components/directory/AssignedRadiosCard";
import { EditUnitDialog } from "../components/directory/EditUnitDialog";
import { RecentRosterCalls } from "../components/directory/RecentRosterCalls";
import { PageHeader } from "../components/layout/PageHeader";
import { Button } from "../components/ui/Button";
import { Card, CardHeader } from "../components/ui/Card";
import { SkeletonText } from "../components/ui/Skeleton";
import { EmptyState, ErrorState } from "../components/ui/States";
import { HELP } from "../help/help";
import { agencyLabel, formatNumber, formatRelative, humanizeName } from "../lib/format";

/** One unit: what it is, which radios it holds, and what those radios have said. */
export function UnitDetail() {
  const { id } = useParams();
  const unitId = Number(id);
  const navigate = useNavigate();
  const [editing, setEditing] = useState(false);
  const { canWrite } = useCurrentUser();
  const unit = useUnit(Number.isFinite(unitId) ? unitId : null);

  if (unit.isLoading) return <SkeletonText lines={8} className="p-6" />;
  if (unit.error) return <ErrorState error={unit.error} onRetry={() => void unit.refetch()} />;
  if (!unit.data) return <EmptyState title="Unit not found" />;

  const detail = unit.data;
  const radioPks = detail.radios.map((radio) => radio.radio_pk);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title={detail.callsign}
        description={`${detail.name ?? "No name recorded"}. ${humanizeName(detail.kind)}, ${formatNumber(detail.radio_count)} ${detail.radio_count === 1 ? "radio" : "radios"} assigned.`}
        actions={
          <>
            <Button tip="Go back" size="sm" variant="ghost" onClick={() => void navigate(-1)}>
              <ArrowLeft size={14} aria-hidden="true" /> Back
            </Button>
            <Button
              tip={canWrite ? HELP["roster.units.edit"] : HELP["app.readOnly"]}
              size="sm"
              disabled={!canWrite}
              onClick={() => setEditing(true)}
            >
              <Pencil size={14} aria-hidden="true" /> Edit
            </Button>
            <Link
              to="/units"
              className="inline-flex h-8 items-center rounded-md border border-line px-2.5 text-sm text-ink hover:bg-surface-2"
            >
              All units
            </Link>
          </>
        }
      />

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto scrollbar-thin p-4 pb-40 md:p-6">
        <div className="grid gap-4 lg:grid-cols-[20rem_1fr]">
          <Card>
            <CardHeader title="Unit" hint={HELP["roster.units.table.callsign"]} />
            <dl className="grid grid-cols-2 gap-x-3 gap-y-2 p-4 text-sm">
              <dt className="text-ink-2">Callsign</dt>
              <dd className="text-ink">{detail.callsign}</dd>
              <dt className="text-ink-2">Name</dt>
              <dd className="text-ink">{detail.name ?? "—"}</dd>
              <dt className="text-ink-2">Kind</dt>
              <dd className="text-ink">{humanizeName(detail.kind)}</dd>
              <dt className="text-ink-2">Agency</dt>
              <dd className="text-ink">{agencyLabel(detail.agency_code, { name: detail.agency_name })}</dd>
              <dt className="text-ink-2">Transmissions</dt>
              <dd className="tabular-nums text-ink">{formatNumber(detail.call_count)}</dd>
              <dt className="text-ink-2">Last heard</dt>
              <dd className="text-ink">{formatRelative(detail.last_call_at)}</dd>
            </dl>
            {detail.notes ? (
              <p className="border-t border-line p-4 text-xs text-ink-2">{detail.notes}</p>
            ) : null}
          </Card>

          <AssignedRadiosCard
            radios={detail.radios}
            hint="Open a radio and use its Assignment card to put it on this unit."
          />
        </div>

        <RecentRosterCalls radioPks={radioPks} />
      </div>

      <EditUnitDialog unit={detail} open={editing} onOpenChange={setEditing} />
    </div>
  );
}
