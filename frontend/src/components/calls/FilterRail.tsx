import { RotateCcw, X } from "lucide-react";
import { useMemo } from "react";
import { useAgencies, useAgencyLabel, useRadios, useTalkgroups } from "../../api/hooks";
import {
  INGEST_PRIORITY_CLASSES,
  QUALITIES,
  TALKGROUP_CATEGORIES,
  TRANSCRIPT_STATUSES,
} from "../../api/types";
import { HELP } from "../../help/help";
import { cn } from "../../lib/cn";
import {
  CALL_TYPE_LABELS,
  CATEGORY_LABELS,
  DOW_LABELS,
  humanizeName,
  minutesToClock,
  radioLabel,
  talkgroupLabel,
} from "../../lib/format";
import {
  CALLS_PRESETS,
  type CallFilters,
  type DatePreset,
  DURATION_MAX,
  DURATION_MIN,
  PRESET_LABELS,
} from "../../state/filters";
import { InfoDot } from "../help/InfoDot";
import { Button } from "../ui/Button";
import { Chip } from "../ui/Chip";
import { DualRange } from "../ui/DualRange";
import { inputClass } from "../ui/Field";
import { MultiSelect } from "../ui/MultiSelect";
import { Switch } from "../ui/Switch";
import { Tip } from "../ui/Tooltip";

const PRESET_HELP: Record<Exclude<DatePreset, "custom">, keyof typeof HELP> = {
  today: "calls.filter.preset.today",
  yesterday: "calls.filter.preset.yesterday",
  "24h": "calls.filter.preset.24h",
  "7d": "calls.filter.preset.7d",
  "30d": "calls.filter.preset.30d",
  "1y": "calls.filter.preset.1y",
  all: "calls.filter.preset.all",
};

function Section({
  title,
  help,
  children,
}: {
  title: string;
  help: keyof typeof HELP;
  children: React.ReactNode;
}) {
  return (
    <section className="border-b border-line px-3 py-3 last:border-0">
      <h3 className="mb-2 flex items-center gap-1 text-xs font-medium text-ink-2">
        {title}
        <InfoDot id={help} />
      </h3>
      {children}
    </section>
  );
}

/**
 * The filter rail. Every control writes straight through to the URL, so the
 * whole view is one link — which is how a shift hands a search over.
 */
export function FilterRail({
  filters,
  patch,
  reset,
  activeCount,
  onClose,
  embedded = false,
  className,
}: {
  filters: CallFilters;
  patch: (changes: Partial<CallFilters>) => void;
  reset: () => void;
  activeCount: number;
  /** Present only in the mobile sheet, where the rail needs a way out. */
  onClose?: () => void;
  /**
   * Inside a dialog that already has a title and a close button: the rail's own header
   * would be a second of each, so only its "clear" action is kept, as a small row.
   */
  embedded?: boolean;
  className?: string;
}) {
  const talkgroups = useTalkgroups();
  const radios = useRadios();
  const agencies = useAgencies();
  const agencyName = useAgencyLabel();

  const talkgroupOptions = useMemo(
    () =>
      // The option's value is the **on-air tgid**, because that is what `?talkgroup=`
      // means. It used to be the row's primary key, which the API also accepted — and
      // since one talkgroup's pk is often another's tgid, picking a channel returned a
      // second, unrelated one alongside it.
      (talkgroups.data?.items ?? []).map((tg) => ({
        value: String(tg.tgid),
        label: `${talkgroupLabel(tg)} · ${tg.tgid}`,
        group: tg.group_name ?? "Other",
        count: tg.call_count,
        // Recently heard first: the local county's channels lead, not the alphabet.
        lastHeard: tg.last_seen_at,
      })),
    [talkgroups.data],
  );

  const radioOptions = useMemo(
    () =>
      (radios.data?.items ?? []).map((radio) => ({
        value: String(radio.id),
        label: radioLabel(radio),
        group: radio.agency_code ? agencyName(radio.agency_code) : "Unassigned",
        count: radio.call_count,
      })),
    [radios.data, agencyName],
  );

  const timeFrom = filters.time_from ?? "00:00";
  const timeTo = filters.time_to ?? "23:59";
  const toMinutes = (clock: string) => Number(clock.slice(0, 2)) * 60 + Number(clock.slice(3, 5));

  return (
    <div className={cn("flex min-h-0 flex-col bg-surface", className)}>
      {embedded ? (
        <div className="flex items-center justify-between gap-2 border-b border-line px-4 py-1.5">
          <p className="text-xs tabular-nums text-ink-2">
            {activeCount > 0 ? `${activeCount} active` : "No filters active"}
          </p>
          <Button
            tip={HELP["calls.filter.reset"]}
            size="sm"
            variant="ghost"
            onClick={reset}
            disabled={activeCount === 0}
          >
            <RotateCcw size={13} aria-hidden="true" /> Clear all
          </Button>
        </div>
      ) : (
        <div className="flex items-center justify-between gap-2 border-b border-line px-3 py-2">
          <Tip label={HELP["calls.filter.activeCount"]}>
            <h2 tabIndex={0} className="text-sm font-semibold text-ink">
              Filters
              {activeCount > 0 ? (
                <span className="ml-1.5 rounded-full bg-accent-fill px-1.5 text-[11px] font-semibold tabular-nums text-white">
                  {activeCount}
                </span>
              ) : null}
            </h2>
          </Tip>
          <div className="flex items-center gap-1">
            <Button
              tip={HELP["calls.filter.reset"]}
              size="iconSm"
              variant="ghost"
              aria-label="Clear every filter"
              onClick={reset}
              disabled={activeCount === 0}
            >
              <RotateCcw size={14} aria-hidden="true" />
            </Button>
            {onClose ? (
              <Button
                tip={HELP["calls.filter.close"]}
                size="iconSm"
                variant="ghost"
                aria-label="Close the filters"
                onClick={onClose}
              >
                <X size={15} aria-hidden="true" />
              </Button>
            ) : null}
          </div>
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto scrollbar-thin">
        <Section title="Transcript text" help="calls.filter.q">
          <Tip label={HELP["calls.filter.q"]}>
            <input
              type="search"
              value={filters.q}
              onChange={(event) => patch({ q: event.target.value })}
              placeholder='"structure fire" -training'
              aria-label="Search transcripts, notes and aliases"
              className={inputClass}
            />
          </Tip>
        </Section>

        <Section title="When" help="calls.filter.panel">
          <div className="flex flex-wrap gap-1.5">
            {/* The rail's own ranges, plus a longer one a shared link named, so the
                selected range is always visible and can be switched away from. */}
            {[
              ...CALLS_PRESETS,
              ...(filters.preset !== "custom" && !CALLS_PRESETS.includes(filters.preset)
                ? [filters.preset]
                : []),
            ].map((preset) => (
              <Chip
                key={preset}
                label={PRESET_LABELS[preset]}
                tip={HELP[PRESET_HELP[preset as Exclude<DatePreset, "custom">]]}
                active={filters.preset === preset}
                onClick={() => patch({ preset })}
              />
            ))}
            <Chip
              label="Custom"
              tip="Pick your own start and end dates"
              active={filters.preset === "custom"}
              onClick={() => patch({ preset: "custom" })}
            />
          </div>
          {filters.preset === "custom" ? (
            <div className="mt-2 grid grid-cols-2 gap-2">
              <Tip label={HELP["calls.filter.dateFrom"]}>
                <input
                  type="date"
                  value={filters.date_from}
                  aria-label="From date"
                  onChange={(event) => patch({ date_from: event.target.value })}
                  className={inputClass}
                />
              </Tip>
              <Tip label={HELP["calls.filter.dateTo"]}>
                <input
                  type="date"
                  value={filters.date_to}
                  aria-label="To date"
                  onChange={(event) => patch({ date_to: event.target.value })}
                  className={inputClass}
                />
              </Tip>
            </div>
          ) : null}
        </Section>

        <Section title="Time of day" help="calls.filter.timeOfDay">
          <DualRange
            min={0}
            max={1439}
            step={15}
            low={toMinutes(timeFrom)}
            high={toMinutes(timeTo)}
            onChange={(low, high) => patch({ time_from: minutesToClock(low), time_to: minutesToClock(high) })}
            format={minutesToClock}
            labelLow="Earliest time of day"
            labelHigh="Latest time of day"
            tip={HELP["calls.filter.timeOfDay"]}
          />
          {filters.time_from ? (
            <Button
              tip="Cover the whole day again"
              size="sm"
              variant="ghost"
              className="mt-1"
              onClick={() => patch({ time_from: null, time_to: null })}
            >
              Any time
            </Button>
          ) : null}
        </Section>

        <Section title="Day of week" help="calls.filter.dow">
          <div className="flex flex-wrap gap-1">
            {DOW_LABELS.map((label, day) => (
              <Chip
                key={label}
                label={label}
                tip={`Keep only calls on a ${label === "Sun" ? "Sunday" : label === "Mon" ? "Monday" : label === "Tue" ? "Tuesday" : label === "Wed" ? "Wednesday" : label === "Thu" ? "Thursday" : label === "Fri" ? "Friday" : "Saturday"}`}
                active={filters.dow.includes(day)}
                onClick={() =>
                  patch({
                    dow: filters.dow.includes(day)
                      ? filters.dow.filter((value) => value !== day)
                      : [...filters.dow, day],
                  })
                }
              />
            ))}
          </div>
        </Section>

        <Section title="Who" help="calls.filter.talkgroup">
          <div className="flex flex-wrap gap-2">
            <MultiSelect
              label="Talkgroups"
              tip={HELP["calls.filter.talkgroup"]}
              options={talkgroupOptions}
              selected={filters.talkgroup.map(String)}
              onChange={(next) => patch({ talkgroup: next.map(Number) })}
              searchPlaceholder="Alias or talkgroup id…"
            />
            <MultiSelect
              label="Radios"
              tip={HELP["calls.filter.radio"]}
              options={radioOptions}
              selected={filters.radio.map(String)}
              onChange={(next) => patch({ radio: next.map(Number) })}
              searchPlaceholder="Unit or radio id…"
            />
            <MultiSelect
              label="Agencies"
              tip={HELP["calls.filter.agency"]}
              options={(agencies.data ?? []).map((agency) => ({
                value: agency.code,
                label: humanizeName(agency.name),
                count: agency.call_count,
              }))}
              selected={filters.agency}
              onChange={(next) => patch({ agency: next })}
            />
            <MultiSelect
              label="Categories"
              tip={HELP["calls.filter.category"]}
              options={TALKGROUP_CATEGORIES.map((category) => ({
                value: category,
                label: CATEGORY_LABELS[category] ?? category,
              }))}
              selected={filters.category}
              onChange={(next) => patch({ category: next })}
            />
          </div>
        </Section>

        <Section title="Call" help="calls.filter.callType">
          <div className="flex flex-wrap gap-1.5">
            {(["group", "patch", "individual"] as const).map((type) => (
              <Chip
                key={type}
                label={CALL_TYPE_LABELS[type]}
                tip={
                  type === "group"
                    ? "Ordinary talkgroup traffic"
                    : type === "patch"
                      ? "Traffic on a temporary supergroup of several talkgroups"
                      : "One radio calling another directly"
                }
                active={filters.call_type === type}
                onClick={() => patch({ call_type: filters.call_type === type ? null : type })}
              />
            ))}
          </div>
          {/* A separate axis from the three above: `call_type` says how the channel was
              granted, this says who the traffic was addressed to. Neither chip on means
              both kinds, which is the reading a timeline wants. */}
          <div className="mt-2 flex flex-wrap gap-1.5">
            {(["group", "unit"] as const).map((kind) => (
              <Chip
                key={kind}
                label={kind === "unit" ? "to one radio" : "to a talkgroup"}
                tip={HELP["calls.filter.kind"]}
                active={filters.kind === kind}
                onClick={() => patch({ kind: filters.kind === kind ? null : kind })}
              />
            ))}
          </div>
          {/* A grid, not inline flow: inline switches ran their labels into the next control. */}
          <div className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2">
            <Switch
              checked={filters.emergency}
              onCheckedChange={(next) => patch({ emergency: next })}
              label="Emergency only"
              tip={HELP["calls.filter.emergency"]}
            />
            <Switch
              checked={filters.encrypted}
              onCheckedChange={(next) => patch({ encrypted: next })}
              label="Encrypted only"
              tip={HELP["calls.filter.encrypted"]}
            />
            <Switch
              checked={filters.alert}
              onCheckedChange={(next) => patch({ alert: next, tone_detected: false })}
              label="Pages only"
              tip={HELP["calls.filter.alert"]}
            />
            <Switch
              checked={filters.tone_detected}
              onCheckedChange={(next) => patch({ tone_detected: next, alert: false })}
              label="Any tone"
              tip={HELP["calls.filter.tone"]}
            />
          </div>
          <div className="mt-3">
            <DualRange
              min={DURATION_MIN}
              max={DURATION_MAX}
              step={0.5}
              low={filters.min_duration}
              high={filters.max_duration}
              onChange={(low, high) => patch({ min_duration: low, max_duration: high })}
              format={(value) => `${value}s`}
              labelLow="Shortest call"
              labelHigh="Longest call"
              tip={HELP["calls.filter.duration"]}
            />
          </div>
        </Section>

        <Section title="Transcript" help="calls.filter.transcriptStatus">
          <div className="flex flex-wrap gap-1">
            {TRANSCRIPT_STATUSES.map((status) => (
              <Chip
                key={status}
                label={status}
                tip={HELP["calls.filter.transcriptStatus"]}
                active={filters.transcript_status.includes(status)}
                onClick={() =>
                  patch({
                    transcript_status: filters.transcript_status.includes(status)
                      ? filters.transcript_status.filter((value) => value !== status)
                      : [...filters.transcript_status, status],
                  })
                }
              />
            ))}
          </div>
          <div className="mt-2 flex flex-wrap gap-1">
            {QUALITIES.map((quality) => (
              <Chip
                key={quality}
                label={quality}
                tip={HELP["calls.filter.quality"]}
                active={filters.quality.includes(quality)}
                onClick={() =>
                  patch({
                    quality: filters.quality.includes(quality)
                      ? filters.quality.filter((value) => value !== quality)
                      : [...filters.quality, quality],
                  })
                }
              />
            ))}
          </div>
        </Section>

        <Section title="How it arrived" help="calls.filter.priority">
          <div className="flex flex-wrap gap-1">
            {INGEST_PRIORITY_CLASSES.map((priority) => (
              <Chip
                key={priority}
                label={priority}
                tip={HELP["calls.filter.priority"]}
                active={filters.priority.includes(priority)}
                onClick={() =>
                  patch({
                    priority: filters.priority.includes(priority)
                      ? filters.priority.filter((value) => value !== priority)
                      : [...filters.priority, priority],
                  })
                }
              />
            ))}
          </div>
        </Section>

        <Section title="Yours" help="calls.filter.starred">
          <div className="grid grid-cols-2 gap-x-3 gap-y-2">
            <Switch
              checked={filters.starred}
              onCheckedChange={(next) => patch({ starred: next })}
              label="Starred only"
              tip={HELP["calls.filter.starred"]}
            />
            <Switch
              checked={filters.include_duplicates}
              onCheckedChange={(next) => patch({ include_duplicates: next })}
              label="Include duplicates"
              tip={HELP["calls.filter.includeDuplicates"]}
            />
            {filters.tag.length ? (
              <div className="col-span-2 flex flex-wrap gap-1 pt-1">
                {filters.tag.map((tag) => (
                  <Chip
                    key={tag}
                    label={tag}
                    tip={HELP["calls.filter.tags"]}
                    active
                    onClick={() => patch({ tag: filters.tag.filter((value) => value !== tag) })}
                  />
                ))}
              </div>
            ) : null}
          </div>
        </Section>
      </div>

      {onClose ? (
        <div className="border-t border-line p-3">
          <Button tip={HELP["calls.filter.apply"]} variant="primary" className="w-full" onClick={onClose}>
            Show results
          </Button>
        </div>
      ) : null}
    </div>
  );
}
