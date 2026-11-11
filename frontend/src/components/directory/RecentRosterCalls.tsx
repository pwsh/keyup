import { useCalls } from "../../api/hooks";
import { HELP } from "../../help/help";
import { callTalkgroupLabel, formatCallTime, formatDuration } from "../../lib/format";
import { usePlayerStore } from "../../state/player";
import { Card, CardHeader } from "../ui/Card";
import { QualityDot } from "../ui/QualityDot";
import { SkeletonText } from "../ui/Skeleton";
import { EmptyState } from "../ui/States";

/**
 * Traffic from the radios a unit or a person currently holds.
 *
 * The calls list already filters on a set of radio ids, so this is one ordinary
 * `GET /calls` — no new endpoint, and nothing added to the calls query itself.
 * It is only as true as the assignments are: a radio reassigned yesterday takes
 * its whole history with it, which is why the card says "currently assigned".
 */
export function RecentRosterCalls({ radioPks }: { radioPks: number[] }) {
  const calls = useCalls({ radio: radioPks, page_size: 50 }, radioPks.length > 0);
  const items = calls.data?.items ?? [];
  const select = usePlayerStore((state) => state.select);
  const setQueue = usePlayerStore((state) => state.setQueue);

  return (
    <Card>
      <CardHeader title="Recent transmissions" hint={HELP["roster.detail.calls"]} />
      {radioPks.length === 0 ? (
        <EmptyState title="No radios assigned yet" />
      ) : calls.isLoading ? (
        <SkeletonText lines={6} className="p-4" />
      ) : items.length === 0 ? (
        <EmptyState title="Nothing recorded from these radios yet" />
      ) : (
        <ul className="divide-y divide-line">
          {items.map((call) => (
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
                <span className="w-40 shrink-0 truncate text-ink-2">{callTalkgroupLabel(call)}</span>
                <span className="min-w-0 flex-1 truncate text-ink">
                  {call.transcript_text ?? <span className="text-muted">{call.transcript_status}</span>}
                </span>
                <QualityDot quality={call.transcript_quality} />
                <span className="shrink-0 tabular-nums text-muted">{formatDuration(call.duration_s)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
