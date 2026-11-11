import { Link } from "react-router";
import type { AssignedRadio } from "../../api/types";
import { HELP } from "../../help/help";
import { formatNumber, formatRelative } from "../../lib/format";
import { Card, CardHeader } from "../ui/Card";
import { EmptyState } from "../ui/States";
import { Tip } from "../ui/Tooltip";

/**
 * The radios a unit or a person is holding right now.
 *
 * Shared by both roster detail pages because the answer has the same shape for
 * each — only the sentence in the empty state differs.
 */
export function AssignedRadiosCard({ radios, hint }: { radios: AssignedRadio[]; hint: string }) {
  return (
    <Card>
      <CardHeader title="Radios assigned" hint={HELP["roster.detail.radios"]} />
      {radios.length === 0 ? (
        <EmptyState title="No radios assigned" hint={hint} />
      ) : (
        <ul className="divide-y divide-line">
          {radios.map((radio) => (
            <li key={radio.assignment_id}>
              <Link
                to={`/radios/${radio.radio_pk}`}
                className="flex items-center gap-3 px-3 py-2 text-sm hover:bg-surface-2"
              >
                <span className="min-w-0 flex-1 truncate text-ink">
                  {radio.unit_id ?? radio.alias ?? radio.radio_id}
                </span>
                <span className="shrink-0 font-mono text-xs tabular-nums text-ink-2">{radio.radio_id}</span>
                <span className="w-20 shrink-0 text-right tabular-nums text-ink-2">
                  {formatNumber(radio.call_count)}
                </span>
                <Tip label="When this radio was last heard">
                  <span tabIndex={0} className="w-24 shrink-0 text-right text-xs text-muted">
                    {formatRelative(radio.last_seen_at)}
                  </span>
                </Tip>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
