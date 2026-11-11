import { ChevronDown, CircleHelp } from "lucide-react";
import { type ReactNode, useState } from "react";
import { useSiteProfile } from "../../api/hooks";
import type { SiteProfile } from "../../api/types";
import { HELP } from "../../help/help";
import { cn } from "../../lib/cn";
import { formatFrequency, formatNumber, formatRelative } from "../../lib/format";
import { SkeletonText } from "../ui/Skeleton";
import { ErrorState } from "../ui/States";
import { Tip } from "../ui/Tooltip";

/** A value that is genuinely absent, drawn as absent rather than left blank. */
const DASH = "—";

function show(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return DASH;
  return String(value);
}

/**
 * One collapsible block of the profile.
 *
 * A heading that is a real `button` with `aria-expanded`, not a `div` with a click
 * handler: the whole panel is a reference document and every section of it has to be
 * reachable from the keyboard.
 */
function Section({
  title,
  count,
  defaultOpen = true,
  children,
}: {
  title: string;
  count?: number;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="border-t border-line first:border-t-0">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((was) => !was)}
        className="flex w-full items-center gap-1.5 px-1 py-2 text-left text-xs font-medium text-ink-2 hover:text-ink"
      >
        <ChevronDown
          size={13}
          aria-hidden="true"
          className={cn("transition-transform", open ? "" : "-rotate-90")}
        />
        {title}
        {count !== undefined ? (
          <span className="font-normal tabular-nums text-muted">{formatNumber(count)}</span>
        ) : null}
      </button>
      {open ? <div className="pb-3 pl-5 pr-1">{children}</div> : null}
    </div>
  );
}

/** A label/value pair of the identity grid. */
function Fact({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] text-ink-2">{label}</dt>
      <dd className="break-words text-xs tabular-nums text-ink">{value}</dd>
    </div>
  );
}

/**
 * One service name. `muted` is the supported-but-not-offered half of the bitmap:
 * the site can do it and is not doing it, which is worth seeing and not worth
 * reading as an offered service.
 */
export function ServiceChip({ name, muted = false }: { name: string; muted?: boolean }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium",
        muted ? "border-line border-dashed text-muted" : "border-line-strong text-ink",
      )}
    >
      {name}
    </span>
  );
}

function TableShell({ head, children }: { head: string[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[22rem] text-left text-xs">
        <thead>
          <tr className="text-xs text-ink-2">
            {head.map((cell) => (
              <th key={cell} scope="col" className="py-1 pr-3 font-semibold">
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

/** Traffic tables long enough to bury the sections under them start folded. */
const TRAFFIC_FOLD_AT = 10;

/**
 * Everything Keyup knows about one tower site, and — the last section — everything it
 * does not.
 *
 * The Sites table has only ever shown the handful of columns the `sites` row carries,
 * while the rest of what a tower announces about itself has been arriving in the
 * observation payloads for months with nowhere to read it: the service bitmap, the
 * band plan, the backup control channels, the neighbours, the packet data. This is
 * that document, folded server-side (`GET /sites/{id}/profile`) and fetched only when
 * a row is expanded.
 *
 * The **Unknown** section is not a footnote. A panel that renders only what is known
 * makes a site with one decoded field look as complete as one with twenty, and the
 * whole point of the unknown-data work is that the holes are the interesting part
 * (docs/UNKNOWN-DATA-PLAN.md).
 */
export function SiteProfilePanel({
  siteId,
  onShowMessageKinds,
}: {
  siteId: number;
  /** Selects this site below, where the message-kind table already lives. */
  onShowMessageKinds?: () => void;
}) {
  const profile = useSiteProfile(siteId);

  if (profile.isLoading) return <SkeletonText lines={6} className="p-4" />;
  if (profile.error) {
    return <ErrorState error={profile.error} onRetry={() => void profile.refetch()} className="py-6" />;
  }
  const data: SiteProfile | undefined = profile.data;
  if (!data) return null;

  const { identity, control, services, message_kinds: kinds } = data;
  const offered = services.available;

  return (
    <div className="px-4 py-2">
      <Section title="Identity">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3 lg:grid-cols-4">
          <Fact label="System" value={show(identity.system_name)} />
          <Fact label="System label on the air" value={show(identity.system_label)} />
          <Fact label="WACN" value={show(identity.wacn)} />
          <Fact label="P25 system id" value={show(identity.p25_system_id)} />
          <Fact
            label="NAC"
            value={identity.nac === null ? DASH : `${identity.nac} (${identity.nac_hex ?? DASH})`}
          />
          <Fact label="RFSS" value={show(identity.rfss)} />
          <Fact label="Site number" value={show(identity.site_number)} />
          <Fact label="LRA" value={show(identity.lra)} />
          <Fact
            label="RadioReference"
            value={
              identity.rr_site_id === null
                ? "not mapped"
                : `site ${identity.rr_site_id}, synced ${formatRelative(identity.rr_synced_at)}`
            }
          />
          <Fact label="Calls recorded" value={formatNumber(data.call_count)} />
          <Fact
            label="Observed by"
            value={
              data.observed_by.length === 0
                ? DASH
                : data.observed_by
                    .map(
                      (row) => `${row.instance ?? "unnamed recorder"} (${formatRelative(row.observed_at)})`,
                    )
                    .join(", ")
            }
          />
          <Fact
            label="Control blocks/s"
            value={control.tsbk_rate === null ? DASH : control.tsbk_rate.toFixed(1)}
          />
        </dl>
        {data.notes ? <p className="mt-2 whitespace-pre-wrap text-xs text-ink-2">{data.notes}</p> : null}
      </Section>

      <Section title="Control channels">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3">
          <Fact label="Primary" value={formatFrequency(control.frequency_hz)} />
          <Fact label="Channel" value={show(control.channel)} />
          <Fact
            label="Recorder hunts"
            value={
              control.configured_control.length === 0
                ? "nothing configured"
                : control.configured_control.map((hz) => formatFrequency(hz)).join(", ")
            }
          />
        </dl>
        {control.secondary_control.length > 0 ? (
          <div className="mt-2">
            <TableShell head={["Secondary control", "Channel", "Service class"]}>
              {control.secondary_control.map((row, index) => (
                <tr key={`${row.channel ?? index}`} className="border-t border-line/60">
                  <td className="py-1 pr-3 tabular-nums text-ink">
                    {row.frequency_hz === null ? (
                      <span className="text-muted">frequency not computable</span>
                    ) : (
                      formatFrequency(row.frequency_hz)
                    )}
                  </td>
                  <td className="py-1 pr-3 text-ink-2">{show(row.channel)}</td>
                  <td className="py-1 pr-3 text-ink-2">{row.service_class.join(", ") || DASH}</td>
                </tr>
              ))}
            </TableShell>
          </div>
        ) : (
          <p className="mt-2 text-xs text-ink-2">No secondary control channel announced.</p>
        )}
      </Section>

      <Section title="Services" count={offered?.length}>
        {offered === null ? (
          <p className="text-xs text-ink-2">
            No SYS_SVC_BCAST has been decoded here, so which services this site offers is unknown.
          </p>
        ) : (
          <>
            <div className="flex flex-wrap gap-1.5">
              {offered.length === 0 ? (
                <span className="text-xs text-ink-2">The bitmap names no available service.</span>
              ) : (
                offered.map((name) => <ServiceChip key={name} name={name} />)
              )}
              {services.supported_not_available.map((name) => (
                <ServiceChip key={`unavailable-${name}`} name={name} muted />
              ))}
            </div>
            <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3">
              <Fact label="Available bitmap" value={show(services.available_raw)} />
              <Fact label="Supported bitmap" value={show(services.supported_raw)} />
              <Fact label="Request priority" value={show(services.request_priority)} />
            </dl>
            {services.supported_not_available.length > 0 ? (
              <p className="mt-1 text-[11px] text-muted">
                Dashed chips are supported but not currently offered.
              </p>
            ) : null}
          </>
        )}
      </Section>

      <Section title="Band plan" count={data.bands.length}>
        {data.bands.length === 0 ? (
          <p className="text-xs text-ink-2">
            No IDEN band plan heard, so an announced channel number cannot be turned into a frequency.
          </p>
        ) : (
          <TableShell head={["Band", "Base", "Spacing", "Bandwidth", "Offset", "TDMA"]}>
            {data.bands.map((band, index) => (
              <tr key={band.id ?? `band-${index}`} className="border-t border-line/60">
                <td className="py-1 pr-3 tabular-nums text-ink">{show(band.id)}</td>
                <td className="py-1 pr-3 tabular-nums text-ink-2">{formatFrequency(band.base_hz)}</td>
                <td className="py-1 pr-3 tabular-nums text-ink-2">
                  {band.spacing_hz === null ? DASH : `${formatNumber(band.spacing_hz)} Hz`}
                </td>
                <td className="py-1 pr-3 tabular-nums text-ink-2">
                  {band.bandwidth_hz === null ? DASH : `${formatNumber(band.bandwidth_hz)} Hz`}
                </td>
                <td className="py-1 pr-3 tabular-nums text-ink-2">
                  {band.offset_hz === null ? DASH : `${(band.offset_hz / 1e6).toFixed(3)} MHz`}
                </td>
                <td className="py-1 pr-3 text-ink-2">
                  {band.tdma === null ? DASH : band.tdma ? "yes" : "no"}
                </td>
              </tr>
            ))}
          </TableShell>
        )}
      </Section>

      <Section title="Adjacent sites" count={data.adjacent_sites.length}>
        {data.adjacent_sites.length === 0 ? (
          <p className="text-xs text-ink-2">This site announces no neighbours in any stored window.</p>
        ) : (
          <TableShell head={["Neighbour", "System", "RFSS / site", "LRA", "Control"]}>
            {data.adjacent_sites.map((row) => (
              <tr key={`${row.system_id}-${row.rfss}-${row.site_number}`} className="border-t border-line/60">
                <td className="py-1 pr-3 text-ink">
                  {row.name === null ? (
                    <span className="rounded border border-dashed border-line px-1.5 py-0.5 text-[11px] text-muted">
                      unnamed
                    </span>
                  ) : (
                    <>
                      {row.name}
                      {/* A tower Keyup has never heard, named by RadioReference: the name is
                          worth far more than "RFSS 20 site 20", and where it came from is
                          worth saying, because Keyup knows nothing else about that site. */}
                      {row.source === "radioreference" ? (
                        <span className="ml-1 text-xs text-muted">RR</span>
                      ) : null}
                    </>
                  )}
                </td>
                <td className="py-1 pr-3 tabular-nums text-ink-2">{show(row.system_id)}</td>
                <td className="py-1 pr-3 tabular-nums text-ink-2">
                  {show(row.rfss)} / {show(row.site_number)}
                </td>
                <td className="py-1 pr-3 tabular-nums text-ink-2">{show(row.lra)}</td>
                <td className="py-1 pr-3 tabular-nums text-ink-2">
                  {formatFrequency(row.control_frequency_hz)}
                </td>
              </tr>
            ))}
          </TableShell>
        )}
      </Section>

      <Section
        title="Traffic channels"
        count={data.traffic.length}
        defaultOpen={data.traffic.length <= TRAFFIC_FOLD_AT}
      >
        {data.traffic.length === 0 ? (
          <p className="text-xs text-ink-2">No voice grant in the newest observation window.</p>
        ) : (
          <TableShell head={["Frequency", "Channel", "Grants", "Calls", "Last heard"]}>
            {data.traffic.map((row, index) => (
              <tr key={row.frequency_hz ?? `traffic-${index}`} className="border-t border-line/60">
                <td className="py-1 pr-3 tabular-nums text-ink">{formatFrequency(row.frequency_hz)}</td>
                <td className="py-1 pr-3 text-ink-2">{show(row.channel)}</td>
                <td className="py-1 pr-3 tabular-nums text-ink-2">{formatNumber(row.grants)}</td>
                <td className="py-1 pr-3 tabular-nums text-ink-2">{formatNumber(row.calls)}</td>
                <td className="py-1 pr-3 text-ink-2">{formatRelative(row.last_seen)}</td>
              </tr>
            ))}
          </TableShell>
        )}
      </Section>

      <Section title="Data channel">
        {data.data_channel === null ? (
          <p className="text-xs text-ink-2">No packet-data activity reported for this site.</p>
        ) : (
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-4">
            <Fact label="Grants" value={formatNumber(data.data_channel.grants)} />
            <Fact label="Pages" value={formatNumber(data.data_channel.pages)} />
            <Fact label="PDUs" value={formatNumber(data.data_channel.pdus)} />
            <Fact
              label="Channels"
              value={
                data.data_channel.channels.length === 0
                  ? DASH
                  : data.data_channel.channels.map((hz) => formatFrequency(hz)).join(", ")
              }
            />
            <Fact label="Low-speed data (non-zero)" value={show(data.lsd_nonzero)} />
          </dl>
        )}
      </Section>

      <Section title="Message kinds" count={kinds.total_seen}>
        <p className="text-xs text-ink-2">
          {formatNumber(kinds.known)} named, {formatNumber(kinds.unknown - kinds.noise)} still unnamed, of{" "}
          {formatNumber(kinds.total_seen)} ever transmitted.{" "}
          {onShowMessageKinds ? (
            <button
              type="button"
              onClick={onShowMessageKinds}
              className="underline underline-offset-2 hover:text-ink"
            >
              Open the message-kind table for this site
            </button>
          ) : null}
        </p>
        {kinds.noise > 0 ? (
          <p className="mt-1 text-[11px] text-muted">
            {formatNumber(kinds.noise)} more were decoded once or twice ever — CRC false passes rather than
            messages, and not counted as unnamed.
          </p>
        ) : null}
      </Section>

      <Section title="Unknown" count={data.unknowns.length}>
        <Tip label={HELP["sites.profile.unknowns"]}>
          <span className="mb-1 inline-flex items-center gap-1 text-[11px] text-ink-2" tabIndex={0}>
            <CircleHelp size={12} aria-hidden="true" /> What this site has not told us
          </span>
        </Tip>
        {data.unknowns.length === 0 ? (
          <p className="text-xs text-ink-2">Nothing outstanding: every field above was decoded.</p>
        ) : (
          <ul className="list-disc space-y-1 pl-4 text-xs text-ink-2">
            {data.unknowns.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        )}
      </Section>
    </div>
  );
}
