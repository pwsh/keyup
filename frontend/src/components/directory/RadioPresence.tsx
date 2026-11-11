import type { RadioAffiliationSource, RadioPresence } from "../../api/types";
import { HELP } from "../../help/help";
import { formatRelative } from "../../lib/format";
import { Card, CardHeader } from "../ui/Card";
import { EmptyState } from "../ui/States";
import { Tip } from "../ui/Tooltip";

/**
 * Where a radio *is*, as opposed to what it has said (docs/RADIO_EVENTS.md).
 *
 * The distinction this block exists to draw is the source badge: an affiliation
 * the control channel announced is a different claim from a channel the radio
 * was merely heard on, and the archive stopped merging the two silently.
 */

const SOURCE_LABEL: Record<RadioAffiliationSource, string> = {
  affiliation: "affiliated",
  call: "heard",
};

const SOURCE_CLASS: Record<RadioAffiliationSource, string> = {
  affiliation: "border-accent text-accent-ink",
  call: "border-line-strong text-ink-2",
};

function SourceBadge({ source }: { source: RadioAffiliationSource }) {
  return (
    <Tip label={HELP["radios.detail.presenceSource"]}>
      <span
        tabIndex={0}
        className={`inline-flex shrink-0 items-center rounded-full border px-1.5 py-0.5 text-[10px] font-medium ${SOURCE_CLASS[source]}`}
      >
        {SOURCE_LABEL[source]}
      </span>
    </Tip>
  );
}

function Stamp({ label, value, tip }: { label: string; value: string | null; tip: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 px-3 py-1.5 text-xs">
      <Tip label={tip}>
        <span tabIndex={0} className="text-ink-2">
          {label}
        </span>
      </Tip>
      <span className="tabular-nums text-muted">{value ? formatRelative(value) : "never"}</span>
    </div>
  );
}

export function RadioPresenceCard({ presence }: { presence: RadioPresence }) {
  const affiliations = presence.affiliations ?? [];
  return (
    <Card>
      <CardHeader title="Presence" hint={HELP["radios.detail.presence"]} />
      <div className="divide-y divide-line">
        <div className="py-1">
          <Stamp
            label="Last seen"
            value={presence.last_seen_at}
            tip="Heard talking, or seen on the control channel"
          />
          <Stamp
            label="Last registered"
            value={presence.last_registered_at}
            tip="When this radio last registered with a site — it was switched on and in range"
          />
          <Stamp
            label="Last de-registered"
            value={presence.last_deregistered_at}
            tip="When the network last acknowledged this radio leaving"
          />
        </div>
        {affiliations.length === 0 ? (
          <EmptyState title="No channels yet" />
        ) : (
          <ul className="divide-y divide-line">
            {affiliations.map((affiliation) => (
              <li
                key={affiliation.talkgroup_id ?? affiliation.tgid}
                className="flex items-center gap-2 px-3 py-2 text-xs"
              >
                <span className="min-w-0 flex-1 truncate text-ink">
                  {affiliation.talkgroup_alias ?? `TG ${affiliation.tgid}`}
                </span>
                <SourceBadge source={affiliation.source} />
                <Tip label="When this channel was last confirmed for this radio">
                  <span tabIndex={0} className="w-20 shrink-0 text-right text-muted">
                    {formatRelative(affiliation.last_affiliated_at ?? affiliation.last_heard_at)}
                  </span>
                </Tip>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}
