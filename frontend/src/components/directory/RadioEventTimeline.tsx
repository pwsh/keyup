import { Link } from "react-router";
import { useSites } from "../../api/hooks";
import type { RadioEvent, RadioEventKind } from "../../api/types";
import { HELP } from "../../help/help";
import { formatCallTime, siteLabel } from "../../lib/format";
import { Card, CardHeader } from "../ui/Card";
import { EmptyState } from "../ui/States";
import { Tip } from "../ui/Tooltip";

/**
 * The control channel's timeline for one radio (docs/RADIO_EVENTS.md).
 *
 * The rows the radio detail already carries — the 50 most recent — rather than a
 * request of its own; `GET /api/radios/{id}/events` is the paged view behind the
 * count in the header.
 */

/** What each `radio_event_kind` is called in the UI. Unknown kinds print raw. */
const KIND_LABEL: Record<RadioEventKind, string> = {
  unit_registration: "registered",
  unit_deregistration: "de-registered",
  unit_registration_command: "told to register",
  group_affiliation: "affiliated",
  group_affiliation_query: "affiliation queried",
  location_registration: "location registered",
  sndcp_grant: "data channel granted",
  sndcp_page: "data page",
  private_call: "private call",
  ack: "acknowledged",
  deny: "denied",
  queued: "queued",
  other: "other",
};

/** Only a refusal is worth coloring; everything else is the normal case. */
const REFUSED = new Set(["deny", "denied", "refused", "fail", "failed"]);

export function RadioEventTimeline({ events, total }: { events: RadioEvent[]; total: number }) {
  const sites = useSites();
  /**
   * The site an event names, as "System / Site".
   *
   * The event carries `site_name` alone, which does not identify a site — two systems can
   * each have a `Springfield` — so the site list is consulted for the system. It falls back to
   * the event's own name when the site has since been deleted or merged away.
   */
  const siteFor = (event: RadioEvent): string => {
    const site = (sites.data ?? []).find((row) => row.id === event.site_id);
    return site ? siteLabel(site) : (event.site_name ?? "");
  };

  return (
    <Card>
      <CardHeader
        title="Control channel"
        hint={HELP["radios.detail.events"]}
        actions={
          total > events.length ? (
            <span className="text-xs tabular-nums text-muted">
              {events.length} of {total}
            </span>
          ) : undefined
        }
      />
      {events.length === 0 ? (
        <EmptyState
          title="Nothing from the control channel yet"
          hint="Registration and affiliation events arrive only from a recorder that sends them"
        />
      ) : (
        <ul className="divide-y divide-line">
          {events.map((event) => (
            <li key={event.id} className="flex items-center gap-3 px-4 py-2 text-xs">
              <span className="w-36 shrink-0 tabular-nums text-ink-2">{formatCallTime(event.at)}</span>
              <span className="w-40 shrink-0 truncate text-ink">{KIND_LABEL[event.kind] ?? event.kind}</span>
              <span className="min-w-0 flex-1 truncate text-ink-2">
                {/* A private call is about a radio, not a talkgroup — so the other end is
                    what belongs here, ahead of the traffic channel it was granted. */}
                {event.kind === "private_call" && event.target_radio_id ? (
                  `→ ${event.target_radio_id}`
                ) : event.talkgroup_pk ? (
                  <Link to={`/talkgroups/${event.talkgroup_pk}`} className="hover:underline">
                    {event.talkgroup_alias ?? `TG ${event.talkgroup_tgid}`}
                  </Link>
                ) : event.frequency_hz ? (
                  `${(event.frequency_hz / 1e6).toFixed(4)} MHz`
                ) : (
                  ""
                )}
              </span>
              {event.result ? (
                <Tip label="What the network answered">
                  <span
                    tabIndex={0}
                    className={`shrink-0 ${REFUSED.has(event.result) ? "text-critical-ink" : "text-muted"}`}
                  >
                    {event.result}
                  </span>
                </Tip>
              ) : null}
              {/* "System / Site": a radio roams, so its events can name sites on two
                  systems, and two systems can each have a site of the same name. */}
              <span className="w-24 shrink-0 truncate text-right text-muted">{siteFor(event) || ""}</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
