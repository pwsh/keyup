import { ArrowLeft, Pencil } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { useAgencyLabel, useCalls, useCurrentUser, useTalkgroup, useTalkgroupStats } from "../api/hooks";
import { BarChart } from "../components/charts/BarChart";
import { EditTalkgroupDialog } from "../components/directory/EditTalkgroupDialog";
import { PageHeader } from "../components/layout/PageHeader";
import { Button } from "../components/ui/Button";
import { Card, CardHeader } from "../components/ui/Card";
import { StatusChip } from "../components/ui/Chip";
import { QualityDot } from "../components/ui/QualityDot";
import { SkeletonText } from "../components/ui/Skeleton";
import { EmptyState, ErrorState } from "../components/ui/States";
import { Tip } from "../components/ui/Tooltip";
import { HELP } from "../help/help";
import { seriesColor } from "../lib/colors";
import {
  categoryLabel,
  formatCallTime,
  formatDuration,
  formatNumber,
  hourLabel,
  talkgroupLabel,
} from "../lib/format";
import { usePlayerStore } from "../state/player";
import { useTheme } from "../state/theme";

/** One channel: its policies, its shape over the day, and who uses it. */
export function TalkgroupDetail() {
  const { id } = useParams();
  const talkgroupId = Number(id);
  const navigate = useNavigate();
  const mode = useTheme();
  const [editing, setEditing] = useState(false);
  const { canWrite } = useCurrentUser();
  const agencyName = useAgencyLabel();

  const talkgroup = useTalkgroup(Number.isFinite(talkgroupId) ? talkgroupId : null);
  const stats = useTalkgroupStats(Number.isFinite(talkgroupId) ? talkgroupId : null);
  // The route carries the row's primary key, but `?talkgroup=` speaks the on-air tgid, so
  // the call list waits for the talkgroup itself rather than passing the id straight
  // through — a pk sent there used to match whichever unrelated channel had it as a tgid.
  const tgid = talkgroup.data?.tgid ?? null;
  const calls = useCalls({ talkgroup: tgid === null ? [] : [tgid], page_size: 100 }, tgid !== null);
  const select = usePlayerStore((state) => state.select);
  const setQueue = usePlayerStore((state) => state.setQueue);

  const hourly = stats.data?.hourly ?? [];
  const topRadios = stats.data?.top_radios ?? [];

  if (talkgroup.isLoading) return <SkeletonText lines={8} className="p-6" />;
  if (talkgroup.error) return <ErrorState error={talkgroup.error} onRetry={() => void talkgroup.refetch()} />;
  if (!talkgroup.data) return <EmptyState title="Talkgroup not found" />;

  const detail = talkgroup.data;
  const items = calls.data?.items ?? [];

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title={talkgroupLabel(detail)}
        description={`Talkgroup ${detail.tgid} on ${detail.group_name ?? "this system"} — ${formatNumber(detail.call_count)} calls recorded.`}
        actions={
          <>
            <Button tip="Go back" size="sm" variant="ghost" onClick={() => void navigate(-1)}>
              <ArrowLeft size={14} aria-hidden="true" /> Back
            </Button>
            {/* Only for someone who can edit; a guest was shown a disabled button. */}
            {canWrite ? (
              <Button tip={HELP["talkgroups.edit"]} size="sm" onClick={() => setEditing(true)}>
                <Pencil size={14} aria-hidden="true" /> Edit
              </Button>
            ) : null}
            <Link
              to={`/calls?talkgroup=${detail.tgid}`}
              className="inline-flex h-8 items-center rounded-md border border-line px-2.5 text-sm text-ink hover:bg-surface-2"
            >
              Filter the calls page
            </Link>
          </>
        }
      />

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto scrollbar-thin p-4 pb-40 md:p-6">
        <Card>
          <div className="flex flex-wrap gap-x-8 gap-y-3 p-4 text-sm">
            <Fact label="Category" help="talkgroups.table.category" value={categoryLabel(detail.category)} />
            <Fact label="Service tag" help="talkgroups.edit.serviceTag" value={detail.service_tag ?? "—"} />
            <Fact label="Agency" help="talkgroups.table.agency" value={agencyName(detail.agency_code)} />
            {/* Queue priority and retention are how this archive is run, not facts about
                the channel: a guest is shown what the channel is and how much was heard. */}
            {canWrite ? (
              <>
                <Fact label="Priority" help="talkgroups.table.priority" value={String(detail.priority)} />
                <Fact
                  label="Transcription"
                  help="talkgroups.table.transcribe"
                  value={
                    detail.transcribe
                      ? `On, for calls over ${detail.transcribe_min_seconds}s`
                      : "Off for this channel"
                  }
                />
                <Fact
                  label="Retention"
                  help="talkgroups.edit.retention"
                  value={detail.retention_days ? `${detail.retention_days} days` : "Archive default"}
                />
              </>
            ) : null}
            <Fact
              label="Audio"
              help="talkgroups.detail.stats"
              value={formatDuration(stats.data?.audio_seconds ?? null)}
            />
            {detail.is_hidden ? <StatusChip status="skipped" tip={HELP["talkgroups.edit.hidden"]} /> : null}
          </div>
        </Card>

        <div className="grid gap-4 lg:grid-cols-2">
          <BarChart
            title="Calls per hour of the day"
            tip={HELP["talkgroups.detail.stats"]}
            labels={hourly.map((point) => hourLabel(point.hour))}
            values={hourly.map((point) => point.count)}
            color={seriesColor(detail.alias ?? "tg", mode)}
            seriesName="Calls"
            isLoading={stats.isLoading}
            isEmpty={hourly.every((point) => point.count === 0)}
            filename={`${detail.alias}-by-hour`}
            data={{
              headers: ["Hour", "Calls"],
              rows: hourly.map((point) => [hourLabel(point.hour), point.count]),
            }}
          />
          <BarChart
            title="Top radios"
            tip={HELP["talkgroups.detail.topRadios"]}
            labels={topRadios.map((radio) => radio.label)}
            values={topRadios.map((radio) => radio.count)}
            color={seriesColor("radios", mode)}
            seriesName="Calls"
            horizontal
            isLoading={stats.isLoading}
            isEmpty={topRadios.length === 0}
            filename={`${detail.alias}-top-radios`}
            data={{
              headers: ["Radio", "Calls"],
              rows: topRadios.map((radio) => [radio.label, radio.count]),
            }}
          />
        </div>

        <Card>
          <CardHeader title="Recent calls" hint={HELP["talkgroups.detail.calls"]} />
          {calls.isLoading ? (
            <SkeletonText lines={6} className="p-4" />
          ) : items.length === 0 ? (
            <EmptyState title="Nothing recorded on this channel yet" />
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
                    <span className="w-24 shrink-0 truncate text-ink-2">{call.radio_label ?? "—"}</span>
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

      {canWrite ? <EditTalkgroupDialog talkgroup={detail} open={editing} onOpenChange={setEditing} /> : null}
    </div>
  );
}

function Fact({ label, help, value }: { label: string; help: keyof typeof HELP; value: string }) {
  return (
    <Tip label={HELP[help]}>
      <div tabIndex={0}>
        <p className="text-xs text-muted">{label}</p>
        <p className="text-ink">{value}</p>
      </div>
    </Tip>
  );
}
