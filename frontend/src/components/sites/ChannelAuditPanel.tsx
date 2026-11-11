import { Copy, Link2, RadioTower, TriangleAlert } from "lucide-react";
import { useSiteChannelAudit } from "../../api/hooks";
import type { ChannelAuditAdjacent, SiteFrequencyRole } from "../../api/types";
import { HELP } from "../../help/help";
import { copyAndToast } from "../../lib/clipboard";
import { formatFrequency, formatRelative } from "../../lib/format";
import { Button } from "../ui/Button";
import { Card, CardHeader } from "../ui/Card";
import { StatusChip } from "../ui/Chip";
import { SkeletonText } from "../ui/Skeleton";
import { EmptyState, ErrorState } from "../ui/States";
import { Tip } from "../ui/Tooltip";

const ROLE_LABEL: Record<SiteFrequencyRole, string> = {
  primary_control: "control",
  alternate_control: "alternate control",
  voice: "voice",
  unknown: "unknown",
};

/** The visual state of a control frequency in the audit. */
type FreqTone = "active" | "primary" | "gap" | "drift" | "quiet";

// A colored state is a filled pill (soft background + border + ink text), so the
// three states read apart by fill — the dark -ink hues alone are too close on the
// light theme (green #0b7d0b, blue #1c5cab, olive #8a5a00 all look similar as text).
const FREQ_TONE: Record<FreqTone, string> = {
  // On the air now — the live control channel.
  active: "border-good bg-good-soft text-good-ink font-semibold",
  // A control channel the recorder is configured to cover.
  primary: "border-accent bg-accent-soft text-accent-ink",
  // Announced by the site but the recorder is not configured for it.
  gap: "border-warning bg-warning-soft text-warning-ink",
  // RadioReference and the air disagree on this one.
  drift: "border-warning bg-warning-soft text-warning-ink underline decoration-dotted",
  quiet: "text-ink-2",
};

// The dot color for the pill and the legend — the solid token, not the -ink text.
const FREQ_DOT: Record<FreqTone, string> = {
  active: "text-good",
  primary: "text-accent",
  gap: "text-warning",
  drift: "text-warning",
  quiet: "text-ink-2",
};

/** A frequency in the one shape every list here uses: monospaced and in MHz. A
 *  toned frequency becomes a filled pill with a leading dot; a plain one is text. */
function Freq({ hz, tone }: { hz: number | null; tone?: FreqTone }) {
  if (!tone || tone === "quiet") {
    return (
      <span className={`font-mono tabular-nums ${tone === "quiet" ? "text-ink-2" : "text-ink"}`}>
        {formatFrequency(hz)}
      </span>
    );
  }
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-xs tabular-nums ${FREQ_TONE[tone]}`}
    >
      <span aria-hidden="true" className={`${FREQ_DOT[tone]} leading-none`}>
        ●
      </span>
      {formatFrequency(hz)}
    </span>
  );
}

/** One labeled row of frequencies in the control-channel block. */
function ControlRow({
  label,
  help,
  values,
  toneFor,
  empty = "none",
}: {
  label: string;
  help: string;
  values: number[];
  toneFor?: (hz: number) => FreqTone | undefined;
  empty?: string;
}) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1 border-b border-line px-4 py-2 last:border-b-0">
      <Tip label={help}>
        <span tabIndex={0} className="w-44 shrink-0 text-xs text-ink-2">
          {label}
        </span>
      </Tip>
      {values.length === 0 ? (
        <span className="text-xs text-muted">{empty}</span>
      ) : (
        values.map((hz) => (
          <span key={hz} className="text-sm">
            <Freq hz={hz} tone={toneFor?.(hz)} />
          </span>
        ))
      )}
    </div>
  );
}

/** Legend for the control-channel colors. */
function ControlLegend() {
  const items: { tone: FreqTone; label: string }[] = [
    { tone: "active", label: "on the air" },
    { tone: "primary", label: "configured" },
    { tone: "gap", label: "announced, not configured" },
    { tone: "drift", label: "differs from RadioReference" },
  ];
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-line px-4 py-2 text-xs text-ink-2">
      {items.map((item) => (
        <span key={item.tone} className="inline-flex items-center gap-1.5">
          <span aria-hidden="true" className={`${FREQ_DOT[item.tone]} text-sm leading-none`}>
            ●
          </span>
          {item.label}
        </span>
      ))}
    </div>
  );
}

function AdjacentRow({ entry }: { entry: ChannelAuditAdjacent }) {
  return (
    <li className="flex flex-wrap items-baseline justify-between gap-2 border-b border-line px-4 py-2 last:border-b-0">
      <span className="text-sm text-ink">
        {entry.known_site_name ? (
          <>
            {entry.known_site_name}
            {entry.known_county ? (
              <span className="ml-1 text-xs text-ink-2">({entry.known_county})</span>
            ) : null}
            <span className="ml-2 text-xs text-ink-2">
              RFSS {entry.rfss} site {entry.site_number}
            </span>
          </>
        ) : (
          <>
            RFSS {entry.rfss} site {entry.site_number}
          </>
        )}
        <span className="ml-2 text-xs text-ink-2">system {entry.system_id_p25}</span>
      </span>
      <span className="flex items-center gap-2">
        <Freq hz={entry.control_frequency_hz} tone="quiet" />
        {entry.known_site_id === null ? (
          <StatusChip status="unknown" tip={HELP["sites.audit.adjacentUnknown"]} />
        ) : (
          <StatusChip
            status={entry.known_in_rr ? "known" : "not in RR"}
            tip={HELP["sites.audit.adjacentKnown"]}
          />
        )}
        <span className="text-xs text-ink-2">{formatRelative(entry.last_seen_at)}</span>
      </span>
    </li>
  );
}

/**
 * The channel audit for one site: the control-channel row, then the three lists
 * that matter — heard but undocumented, documented but not heard, and the
 * neighbors the control channel announces — plus the control-frequency list to
 * paste back into the recorder (docs/CHANNEL_AUDIT.md).
 */
export function ChannelAuditPanel({
  siteId,
  onMapRadioReference,
}: {
  siteId: number | null;
  /** Opens the owning system's edit dialog; omitted where there is nothing to open. */
  onMapRadioReference?: () => void;
}) {
  const audit = useSiteChannelAudit(siteId);

  if (siteId === null) return <EmptyState title="Pick a site" hint="Choose a tower site to audit." />;
  if (audit.isLoading) return <SkeletonText lines={8} className="p-4" />;
  if (audit.error) return <ErrorState error={audit.error} onRetry={() => void audit.refetch()} />;
  if (!audit.data) return <EmptyState title="No audit for this site" />;

  const report = audit.data;
  const drifted = new Set(report.control_drift.map((entry) => entry.frequency_hz));
  const activeHz = report.control.observed;
  const configured = new Set(report.control.configured);
  const notConfigured = new Set(report.recorder_config.announced_not_configured);
  // Most-specific wins: the live channel is green, a gap the recorder misses is
  // amber, a covered control channel is blue, an RR disagreement is flagged, and
  // anything else stays neutral.
  const toneFor = (hz: number): FreqTone | undefined => {
    if (hz === activeHz) return "active";
    if (notConfigured.has(hz)) return "gap";
    if (configured.has(hz)) return "primary";
    if (drifted.has(hz)) return "drift";
    return undefined;
  };
  const suggested = report.recorder_config.suggested_control_frequencies;
  const suggestedText = suggested.map((hz) => (hz / 1e6).toFixed(4)).join(", ");
  const documented = report.rr_synced_at !== null;

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="Control channel"
          hint={
            documented
              ? `RadioReference synced ${formatRelative(report.rr_synced_at)} · last heard ${formatRelative(report.last_observed_at)}`
              : `Not mapped to RadioReference — last heard ${formatRelative(report.last_observed_at)}`
          }
          actions={
            <span className="flex items-center gap-2">
              {/* The panel used to *tell* the operator to map the system and offer no way
                  to do it — the mapping endpoint had no caller in the app at all. This is
                  that missing affordance: it opens the system's edit dialog on the row
                  this audit is about. */}
              {documented || onMapRadioReference === undefined ? null : (
                <Button tip={HELP["sites.audit.map"]} size="sm" onClick={onMapRadioReference}>
                  <Link2 size={13} aria-hidden="true" /> Map to RadioReference
                </Button>
              )}
              {report.control_drift.length > 0 ? (
                <StatusChip status="drifted" tip={HELP["sites.audit.drift"]} />
              ) : null}
            </span>
          }
        />
        <ControlLegend />
        <ControlRow
          label="On the air now"
          help={HELP["sites.audit.controlObserved"]}
          values={report.control.observed === null ? [] : [report.control.observed]}
          toneFor={toneFor}
          empty="nothing heard"
        />
        <ControlRow
          label="Announced alternates"
          help={HELP["sites.audit.controlAlternates"]}
          values={report.control.observed_alternate}
          toneFor={toneFor}
        />
        <ControlRow
          label="RadioReference primary"
          help={HELP["sites.audit.rrPrimary"]}
          values={report.control.rr_primary}
          toneFor={toneFor}
          empty={documented ? "none documented" : "not synced"}
        />
        <ControlRow
          label="RadioReference alternates"
          help={HELP["sites.audit.rrAlternates"]}
          values={report.control.rr_alternate}
          toneFor={toneFor}
          empty={documented ? "none documented" : "not synced"}
        />
        <ControlRow
          label="Recorder is configured for"
          help={HELP["sites.audit.configured"]}
          values={report.control.configured}
          empty="the recorder has not said"
        />
        <div className="flex flex-wrap items-center gap-2 border-t border-line px-4 py-3">
          <Tip label={HELP["sites.audit.suggested"]}>
            <span tabIndex={0} className="text-xs text-ink-2">
              Suggested trunkd control_frequencies
            </span>
          </Tip>
          <code className="min-w-0 flex-1 truncate rounded-md border border-line bg-surface-2 px-2 py-1.5 font-mono text-xs text-ink">
            {suggestedText || "—"}
          </code>
          <Button
            tip={HELP["sites.audit.copy"]}
            size="iconSm"
            disabled={suggested.length === 0}
            onClick={() => {
              void copyAndToast(suggestedText, { label: "Control frequencies" });
            }}
            aria-label="Copy the suggested control frequencies"
          >
            <Copy size={14} aria-hidden="true" />
          </Button>
        </div>
        {report.recorder_config.announced_not_configured.length > 0 ||
        report.recorder_config.configured_not_announced.length > 0 ? (
          <div className="space-y-1 border-t border-line px-4 py-2 text-xs text-ink-2">
            {report.recorder_config.announced_not_configured.length > 0 ? (
              <p>
                <TriangleAlert size={12} className="mr-1 inline text-warning-ink" aria-hidden="true" />
                Announced but not configured:{" "}
                {report.recorder_config.announced_not_configured.map((hz) => (
                  <span key={hz} className="mr-2">
                    <Freq hz={hz} tone="drift" />
                  </span>
                ))}
              </p>
            ) : null}
            {report.recorder_config.configured_not_announced.length > 0 ? (
              <p>
                Configured but no longer announced:{" "}
                {report.recorder_config.configured_not_announced.map((hz) => (
                  <span key={hz} className="mr-2">
                    <Freq hz={hz} tone="quiet" />
                  </span>
                ))}
              </p>
            ) : null}
          </div>
        ) : null}
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader
            title={`Undocumented (${report.undocumented.length})`}
            hint="On the air here, not in RadioReference."
          />
          {report.undocumented.length === 0 ? (
            <EmptyState
              title={documented ? "Everything heard is documented" : "Nothing to compare against yet"}
              hint={
                documented
                  ? undefined
                  : "Map this system to RadioReference — the button above the control channel — to see which channels are undocumented."
              }
              icon={<RadioTower size={24} />}
            />
          ) : (
            <ul>
              {report.undocumented.map((entry) => (
                <li
                  key={entry.frequency_hz}
                  className="flex flex-wrap items-baseline justify-between gap-2 border-b border-line px-4 py-2 last:border-b-0"
                >
                  <span className="text-sm">
                    <Freq hz={entry.frequency_hz} />
                    <span className="ml-2 text-xs text-ink-2">{ROLE_LABEL[entry.role]}</span>
                  </span>
                  <span className="text-xs text-ink-2">
                    {entry.grant_count} grants · heard {formatRelative(entry.last_seen_at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card>
          <CardHeader
            title={`Missing (${report.missing.length})`}
            hint={`In RadioReference, not heard in the last ${report.stale_days} days.`}
          />
          {report.missing.length === 0 ? (
            <EmptyState title="Everything documented is on the air" icon={<RadioTower size={24} />} />
          ) : (
            <ul>
              {report.missing.map((entry) => (
                <li
                  key={entry.frequency_hz}
                  className="flex flex-wrap items-baseline justify-between gap-2 border-b border-line px-4 py-2 last:border-b-0"
                >
                  <span className="text-sm">
                    <Freq hz={entry.frequency_hz} tone="quiet" />
                    <span className="ml-2 text-xs text-ink-2">{ROLE_LABEL[entry.rr_role]}</span>
                  </span>
                  <span className="text-xs text-ink-2">
                    {entry.last_seen_at === null
                      ? "never heard"
                      : `last heard ${formatRelative(entry.last_seen_at)}`}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <Card>
        <CardHeader
          title={`Adjacent sites (${report.adjacent_sites.length})`}
          hint="Neighboring towers this site's control channel announces."
        />
        {report.adjacent_sites.length === 0 ? (
          <EmptyState title="No neighbors announced" icon={<RadioTower size={24} />} />
        ) : (
          <ul>
            {report.adjacent_sites.map((entry) => (
              <AdjacentRow key={`${entry.system_id_p25}-${entry.rfss}-${entry.site_number}`} entry={entry} />
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
