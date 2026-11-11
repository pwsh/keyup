import { ArrowLeft, Pencil } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { useCalls, useCurrentUser, useRadio, useRadioAffiliations, useRadioHistory } from "../api/hooks";
import { isUnitCall } from "../api/types";
import { BarChart } from "../components/charts/BarChart";
import { AssignmentCard } from "../components/directory/AssignmentCard";
import { EditRadioDialog } from "../components/directory/EditRadioDialog";
import { RadioEventTimeline } from "../components/directory/RadioEventTimeline";
import { RadioPresenceCard } from "../components/directory/RadioPresence";
import { PageHeader } from "../components/layout/PageHeader";
import { Button } from "../components/ui/Button";
import { Card, CardHeader } from "../components/ui/Card";
import { QualityDot } from "../components/ui/QualityDot";
import { SkeletonText } from "../components/ui/Skeleton";
import { EmptyState, ErrorState } from "../components/ui/States";
import { Tip } from "../components/ui/Tooltip";
import { HELP } from "../help/help";
import { seriesColor } from "../lib/colors";
import {
  callTalkgroupLabel,
  formatCallTime,
  formatDuration,
  formatNumber,
  formatRelative,
  hourLabel,
  radioLabel,
  unitCallLabel,
} from "../lib/format";
import { usePlayerStore } from "../state/player";
import { useTheme } from "../state/theme";

/** One radio: what it is, where it has been heard, and what it has said. */
export function RadioDetail() {
  const { id } = useParams();
  const radioId = Number(id);
  const navigate = useNavigate();
  const mode = useTheme();
  const [editing, setEditing] = useState(false);
  const { canWrite } = useCurrentUser();

  const radio = useRadio(Number.isFinite(radioId) ? radioId : null);
  const affiliations = useRadioAffiliations(Number.isFinite(radioId) ? radioId : null);
  const history = useRadioHistory(Number.isFinite(radioId) ? radioId : null);
  const calls = useCalls({ radio: [radioId], page_size: 100 }, Number.isFinite(radioId));
  const select = usePlayerStore((state) => state.select);
  const setQueue = usePlayerStore((state) => state.setQueue);

  const hourly = history.data?.hourly ?? [];

  if (radio.isLoading) return <SkeletonText lines={8} className="p-6" />;
  if (radio.error) return <ErrorState error={radio.error} onRetry={() => void radio.refetch()} />;
  if (!radio.data) return <EmptyState title="Radio not found" />;

  const detail = radio.data;
  const items = calls.data?.items ?? [];
  const name = radioLabel(detail);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title={name}
        description={`Radio ${detail.radio_id}${detail.wacn ? ` (home ${detail.wacn}.${detail.home_system_id})` : ""} — ${formatNumber(detail.call_count)} transmissions.`}
        actions={
          <>
            <Button tip="Go back" size="sm" variant="ghost" onClick={() => void navigate(-1)}>
              <ArrowLeft size={14} aria-hidden="true" /> Back
            </Button>
            <Button
              tip={canWrite ? HELP["radios.edit"] : HELP["app.readOnly"]}
              size="sm"
              disabled={!canWrite}
              onClick={() => setEditing(true)}
            >
              <Pencil size={14} aria-hidden="true" /> Edit
            </Button>
            <Link
              to={`/calls?radio=${detail.id}`}
              className="inline-flex h-8 items-center rounded-md border border-line px-2.5 text-sm text-ink hover:bg-surface-2"
            >
              Filter the calls page
            </Link>
          </>
        }
      />

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto scrollbar-thin p-4 pb-40 md:p-6">
        <div className="grid gap-4 lg:grid-cols-[1fr_20rem]">
          <BarChart
            title="Activity by hour"
            tip={HELP["radios.detail.history"]}
            labels={hourly.map((point) => hourLabel(point.hour))}
            values={hourly.map((point) => point.count)}
            color={seriesColor(String(detail.radio_id ?? "radio"), mode)}
            seriesName="Transmissions"
            isLoading={history.isLoading}
            isEmpty={hourly.every((point) => point.count === 0)}
            filename={`${name}-by-hour`}
            data={{
              headers: ["Hour", "Transmissions"],
              rows: hourly.map((point) => [hourLabel(point.hour), point.count]),
            }}
          />

          <Card>
            <CardHeader title="Heard on" hint={HELP["radios.detail.affiliations"]} />
            {affiliations.isLoading ? (
              <SkeletonText lines={5} className="p-4" />
            ) : (affiliations.data ?? []).length === 0 ? (
              <EmptyState title="No affiliations yet" />
            ) : (
              <ul className="divide-y divide-line">
                {(affiliations.data ?? []).map((affiliation) => (
                  <li key={`${affiliation.talkgroup_id}`}>
                    <Link
                      to={`/talkgroups/${affiliation.talkgroup_id}`}
                      className="flex items-center gap-2 px-3 py-2 text-xs hover:bg-surface-2"
                    >
                      <span className="min-w-0 flex-1 truncate text-ink">
                        {affiliation.talkgroup_alias ?? `TG ${affiliation.tgid}`}
                      </span>
                      <span className="tabular-nums text-ink-2">{affiliation.call_count}</span>
                      <Tip label="When this radio was last heard on that channel">
                        <span tabIndex={0} className="w-20 shrink-0 text-right text-muted">
                          {formatRelative(affiliation.last_heard_at)}
                        </span>
                      </Tip>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>

        <AssignmentCard radio={detail} />

        <div className="grid gap-4 lg:grid-cols-[20rem_1fr]">
          <RadioPresenceCard presence={detail.presence} />
          <RadioEventTimeline events={detail.events} total={detail.event_count} />
        </div>

        <Card>
          <CardHeader title="Recent transmissions" hint={HELP["radios.detail.calls"]} />
          {calls.isLoading ? (
            <SkeletonText lines={6} className="p-4" />
          ) : items.length === 0 ? (
            <EmptyState title="Nothing recorded from this radio yet" />
          ) : (
            <ul className="divide-y divide-line">
              {items.slice(0, 50).map((call) => (
                <li key={call.id}>
                  <button
                    type="button"
                    onClick={() => {
                      setQueue(items);
                      select(call);
                    }}
                    className="flex w-full items-center gap-3 px-4 py-2 text-left text-sm hover:bg-surface-2"
                  >
                    <span className="w-36 shrink-0 tabular-nums text-ink-2">
                      {formatCallTime(call.started_at)}
                    </span>
                    <span className="w-40 shrink-0 truncate text-ink-2">
                      {/* A private call is on no talkgroup, so the pair of radios stands
                          where the channel name would. The list holds the calls this radio
                          was *called on* as well as the ones it started — the `radio`
                          filter reads both ends — so the arrow is also what says which of
                          the two this radio was. */}
                      {isUnitCall(call) ? unitCallLabel(call) : callTalkgroupLabel(call)}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-ink">
                      {call.transcript_text ?? <span className="text-muted">{call.transcript_status}</span>}
                    </span>
                    <QualityDot quality={call.transcript_quality} />
                    <span className="shrink-0 tabular-nums text-muted">
                      {formatDuration(call.duration_s)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <EditRadioDialog radio={detail} open={editing} onOpenChange={setEditing} />
    </div>
  );
}
